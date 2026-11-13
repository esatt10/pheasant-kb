import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import { RateTile, StageBars, SweepChart } from "../tuning/charts";
import { Explain, MechanismTable, ObjectiveBanner } from "../tuning/explain";
import type { GlossaryEntry, TuningGate, TuningHealth, TuningTrial } from "../api/types";

/** Stages a ranking parameter can actually move. Mirrors `tuning.stages`. */
const ACTIONABLE = new Set([
  "lexical_candidates",
  "vector_candidates",
  "filters",
  "fusion",
  "truncation",
]);

/** What each stage means, in the words a reader needs rather than the code's. */
const STAGE_HELP: Record<string, string> = {
  absent_from_corpus: "Not indexed. No ranking parameter can reach this.",
  candidates_missing: "No arm returned it, and the corpus was not checked.",
  lexical_candidates: "The text arm did not return it within its fetch window.",
  vector_candidates: "The vector arm did not return it.",
  graph_candidates: "The graph arm did not return it.",
  filters: "An arm had it and a filter removed it — ACL, artifact policy, or section.",
  fusion: "It survived every filter and the merge ranked it below the cut.",
  truncation: "Fused above the cut, then deduplicated away.",
  served: "Returned. Nothing failed.",
};

const TERMINAL = new Set(["completed", "failed", "interrupted", "none"]);

/**
 * Which step of retrieval is failing, and what to do about it.
 *
 * The page is organised around one claim: an absent result has six possible
 * causes, they look identical after the merge, and they call for different
 * responses. So the histogram is the headline — not a score — and every stage
 * on it is labelled with whether a parameter can reach it at all. A region
 * whose misses are mostly `absent_from_corpus` should leave this page and go
 * look at indexing, and the page says so rather than offering a tuning button
 * that cannot help.
 *
 * Progress comes from `/tuning/status`, which reads `/state` rather than the
 * process running the batch — so this works when the browser is talking to a
 * replica that did not start it, and after the container that did was
 * restarted.
 */
export function TuningPage() {
  const queryClient = useQueryClient();
  const [baselineName, setBaselineName] = useState("original-base");
  const baseline = useMutation({
    mutationFn: () => api.saveRetrievalBaseline(baselineName),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["tuning", "bundles"] });
      void queryClient.invalidateQueries({ queryKey: ["artifacts"] });
    },
  });
  const [showAllTrials, setShowAllTrials] = useState(false);

  const status = useQuery({
    queryKey: ["tuning", "status"],
    queryFn: () => api.tuningStatus(),
    // Polled only while something is actually running: a page left open on a
    // finished batch should not keep a database read going forever.
    refetchInterval: (query) =>
      TERMINAL.has(query.state.data?.status ?? "none") ? false : 2000,
  });
  const running = status.data?.status === "running";

  const report = useQuery({
    queryKey: ["tuning", "report", status.data?.experiment_id, status.data?.status],
    queryFn: () => api.tuningReport(),
    // 404 until a batch has finished, which is a normal state and not an error
    // worth retrying at.
    retry: false,
    enabled: !running,
  });
  const parameters = useQuery({
    queryKey: ["tuning", "parameters"],
    queryFn: () => api.tuningParameters(),
  });
  const bundles = useQuery({
    queryKey: ["tuning", "bundles"],
    queryFn: () => api.tuningBundles(),
  });
  // Experiment observability, served from /state. There is deliberately no
  // link out to a tracking server here: the parameter point, the score, the
  // motivating stage and the rationale are all rows this API already
  // publishes, so the sweep belongs on this page rather than behind a second
  // system somebody has to be running.
  const trials = useQuery({
    queryKey: ["tuning", "trials", status.data?.experiment_id],
    queryFn: () => api.tuningTrials(),
    enabled: !running,
  });
  // Live pipeline behaviour, which needs no batch at all. This is what makes
  // the page useful *between* runs and is the only thing here that can notice
  // a regression the moment a bundle starts serving.
  const health = useQuery({
    queryKey: ["tuning", "health"],
    queryFn: () => api.tuningHealth(),
    refetchInterval: 15000,
  });
  // The explanation catalog. Fetched once and threaded into every panel, so a
  // number and its meaning are never more than a click apart.
  const glossary = useQuery({
    queryKey: ["tuning", "glossary"],
    queryFn: () => api.tuningGlossary(),
    staleTime: Infinity,
  });
  const lineage = useQuery({
    queryKey: ["tuning", "lineage"],
    queryFn: () => api.tuningLineage(),
  });

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["tuning"] });
  };
  const start = useMutation({
    mutationFn: (body: { diagnose_only?: boolean }) =>
      api.tuningRun({ force: true, ...body }),
    onSuccess: invalidate,
  });
  const apply = useMutation({
    mutationFn: (bundleId: string) => api.tuningApply(bundleId),
    onSuccess: invalidate,
  });
  const rollback = useMutation({
    mutationFn: (to: string) => api.tuningRollback(to),
    onSuccess: invalidate,
  });
  const cancel = useMutation({
    mutationFn: () => api.tuningCancel(),
    onSuccess: invalidate,
  });
  const pin = useMutation({
    mutationFn: (names: string[]) => api.tuningPin(names),
    onSuccess: invalidate,
  });
  const prune = useMutation({
    mutationFn: (id: string) => api.tuningPrune(id),
    onSuccess: invalidate,
  });

  const active = parameters.data?.active;
  const decision = report.data?.decision;
  const histogram = report.data?.diagnosis?.histogram;
  const pinned = parameters.data?.space.pinned ?? [];
  const explain = (term: string): GlossaryEntry | undefined => {
    const catalog = glossary.data;
    if (!catalog) return undefined;
    for (const group of [
      catalog.health,
      catalog.stages,
      catalog.gates,
      catalog.metrics,
      catalog.parameters,
    ]) {
      const found = group.find((entry) => entry.term === term);
      if (found) return found;
    }
    return undefined;
  };

  return (
    <div className="page tune-page">
      <header className="page__header">
        <div>
          <h1>Retrieval tuning</h1>
          <p className="page__lede">
            Which step of retrieval is losing documents, and whether a parameter can
            reach it. Producing a configuration bundle changes nothing; applying one
            re-ranks every replica.
          </p>
        </div>
        <div className="page__actions">
          <label>Baseline name <input className="text-input" value={baselineName}
            onChange={event => setBaselineName(event.target.value)} /></label>
          <button className="btn" disabled={!baselineName.trim() || baseline.isPending}
            onClick={() => baseline.mutate()}>Save configured base</button>
          {baseline.data ? <span className="muted small">Saved {baseline.data.bundle_id}; apply it from Bundles.</span> : null}
          {baseline.isError ? <span className="error">{(baseline.error as Error).message}</span> : null}
          <button
            type="button"
            className="btn"
            onClick={() => start.mutate({ diagnose_only: true })}
            disabled={running || start.isPending}
          >
            Diagnose only
          </button>
          <button
            type="button"
            className="btn btn--primary"
            onClick={() => start.mutate({})}
            disabled={running || start.isPending}
          >
            {running ? "Running…" : "Run a tuning batch"}
          </button>
        </div>
      </header>

      {status.data && status.data.status !== "none" ? (
        <section className="tune-section">
          <h2>Batch</h2>
          <div className="tuning__status">
            <span className={`badge badge--${status.data.status}`}>{status.data.status}</span>
            <span>{status.data.phase}</span>
            <span className="muted">{status.data.phase_detail}</span>
          </div>
          {status.data.total_units > 0 ? (
            <div className="eval-progress__bar">
              <div
                className="eval-progress__fill"
                style={{ width: `${Math.round((status.data.progress ?? 0) * 100)}%` }}
              />
              <span className="muted small">
                {status.data.completed_units}/{status.data.total_units} points ·{" "}
                {status.data.searches} searches
              </span>
            </div>
          ) : null}
          {status.data.attempts > 1 ? (
            <p className="muted">
              Attempt {status.data.attempts}: an earlier one was interrupted and this
              batch resumed from its stored trials.
            </p>
          ) : null}
          {running ? (
            <button
              type="button"
              className="btn"
              onClick={() => cancel.mutate()}
              disabled={cancel.isPending}
            >
              Cancel this batch
            </button>
          ) : null}
          {status.data.status === "cancelled" ? (
            <p className="muted small">
              Cancelled. Its trials are stored, so running again resumes rather than
              starting over.
            </p>
          ) : null}
          {status.data.error ? <p className="muted small tune-error">{status.data.error}</p> : null}
        </section>
      ) : null}

      <ObjectiveBanner
        objective={parameters.data?.objective ?? report.data?.objective}
        baselineSubstituted={report.data?.objective?.baseline_substituted}
      />

      <LiveHealth health={health.data} explain={explain} />

      <section className="tune-section">
        <h2>What this region ranks with</h2>
        {active ? (
          <>
            {/* Base and overlay reported separately, never collapsed. "What is
                it ranking with" and "what would it rank with if I rolled back"
                are different questions, and the second is asked at exactly the
                moment somebody is least able to go and look it up. */}
            <div className="layers">
              <div className="layer">
                <div className="layer__name">Base configuration</div>
                <p className="muted small">{active.base.explanation}</p>
                <p className="muted small">
                  Source: <code>{active.base.source}</code>
                </p>
              </div>
              <div className={active.overlay.bundle_id ? "layer layer--live" : "layer layer--off"}>
                <div className="layer__name">
                  Promoted overlay{active.overlay.bundle_id ? "" : " — none"}
                </div>
                <p className="muted small">{active.overlay.explanation}</p>
                {active.overlay.bundle_id ? (
                  <p className="muted small">
                    <code>{active.overlay.bundle_id}</code> applied{" "}
                    {active.overlay.applied_at} by {active.overlay.applied_by}
                  </p>
                ) : null}
              </div>
            </div>
            {active.changes.length ? (
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Changed by the overlay</th>
                    <th>Base</th>
                    <th>Serving</th>
                    <th>Stage</th>
                  </tr>
                </thead>
                <tbody>
                  {active.changes.map((change) => (
                    <tr key={change.parameter}>
                      <td>
                        <code>{change.parameter}</code>
                      </td>
                      <td className="muted">{change.base}</td>
                      <td>
                        <strong>{change.active}</strong>
                      </td>
                      <td className="muted">{change.stage}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : null}
            <p>
              Parameters come from <strong>{active.provenance}</strong>
              {active.bundle_id ? (
                <>
                  {" "}
                  — bundle <code>{active.bundle_id}</code>, applied{" "}
                  {active.bundle?.applied_at} by {active.bundle?.applied_by}
                </>
              ) : (
                " — the values in pheasant.yaml"
              )}
              .
            </p>
            {active.provenance === "bundle" ? (
              <button
                type="button"
                className="btn"
                onClick={() => rollback.mutate("base")}
                disabled={rollback.isPending}
              >
                Roll back to configured values
              </button>
            ) : null}
            <table className="data-table">
              <thead>
                <tr>
                  <th>Parameter</th>
                  <th>Value</th>
                  <th>Stage it acts on</th>
                  <th>Pinned</th>
                </tr>
              </thead>
              <tbody>
                {parameters.data?.space.parameters.map((spec) => {
                  const isPinned = pinned.includes(spec.name);
                  return (
                    <tr key={spec.name}>
                      <td>
                        <code>{spec.name}</code>
                        <Explain entry={explain(spec.name)} compact />
                      </td>
                      <td>{active.values[spec.name]}</td>
                      <td>
                        {spec.stage}{" "}
                        <span className="muted">
                          ({spec.cost_class === "refusion" ? "free to trial" : "needs a search"})
                        </span>
                      </td>
                      <td>
                        {/* Pinning is how an operator says "I have measured
                            this one, stop re-litigating it". Persisted, so the
                            next scheduled batch honours it too. */}
                        <input
                          type="checkbox"
                          checked={isPinned}
                          aria-label={`Pin ${spec.name}`}
                          onChange={() =>
                            pin.mutate(
                              isPinned
                                ? pinned.filter((n) => n !== spec.name)
                                : [...pinned, spec.name],
                            )
                          }
                        />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </>
        ) : (
          <p className="muted">Loading…</p>
        )}
      </section>

      {histogram ? (
        <section className="tune-section">
          <h2>Where retrieval loses documents</h2>
          <p className="page__lede">{report.data?.diagnosis.summary}</p>
          <StageBars
            histogram={histogram}
            actionable={ACTIONABLE}
            help={STAGE_HELP}
            explain={explain}
          />
          <MechanismTable
            mechanisms={report.data?.mechanisms}
            objectiveLabel={report.data?.objective?.label ?? "score"}
          />
          {histogram.actionable_share !== null && histogram.actionable_share < 0.34 ? (
            <p className="tune-warning">
              Most misses are in stages no retrieval parameter reaches. Tuning is the
              wrong tool here — the failures are upstream, in what is indexed and how it
              is chunked.
            </p>
          ) : null}
        </section>
      ) : null}

      {decision ? (
        <section className="tune-section">
          <h2>Decision</h2>
          <p>
            <span className={`badge badge--${decision.outcome}`}>{decision.outcome}</span>{" "}
            {decision.reason}
          </p>
          <ul className="eval-gates">
            {decision.gates.map((gate) => (
              <GateRow key={gate.gate_id} gate={gate} entry={explain(gate.gate_id)} />
            ))}
          </ul>
          {decision.comparisons.length ? (
            <table className="data-table">
              <thead>
                <tr>
                  <th>Comparison</th>
                  <th>Delta</th>
                  <th>Paired queries</th>
                </tr>
              </thead>
              <tbody>
                {decision.comparisons.map((c, index) => (
                  <tr key={`${c.metric}-${index}`}>
                    <td title={c.substituted}>{c.metric}</td>
                    <td>{c.delta >= 0 ? `+${c.delta.toFixed(4)}` : c.delta.toFixed(4)}</td>
                    <td>{c.paired_queries}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : null}
        </section>
      ) : null}

      {report.data?.trials?.length ? (
        <section className="tune-section">
          <h2>Trials</h2>
          <p className="muted">
            {report.data.trial_count} points evaluated with {report.data.searches} searches.
            Fusion parameters cost no retrieval at all — they are re-computed from cached
            candidates — which is why the two counts differ so much.
          </p>
          <table className="data-table">
            <thead>
              <tr>
                <th>{report.data.primary_metric}</th>
                <th>Change</th>
                <th>Stage</th>
                <th>Why it was tried</th>
              </tr>
            </thead>
            <tbody>
              {(showAllTrials ? report.data.trials : report.data.trials.slice(0, 8)).map(
                (trial: TuningTrial) => (
                  <tr key={trial.trial_id}>
                    <td>{(trial.metrics[report.data!.primary_metric] ?? 0).toFixed(4)}</td>
                    <td>
                      <code>{trial.proposal.point.delta_description}</code>
                    </td>
                    <td>{trial.proposal.motivating_stage}</td>
                    <td className="muted">{trial.proposal.rationale}</td>
                  </tr>
                ),
              )}
            </tbody>
          </table>
          {report.data.trials.length > 8 ? (
            <button type="button" className="btn" onClick={() => setShowAllTrials((shown) => !shown)}>
              {showAllTrials ? "Show fewer" : `Show all ${report.data.trials.length}`}
            </button>
          ) : null}
        </section>
      ) : null}

      {trials.data && Object.keys(trials.data.sweeps ?? {}).length ? (
        <section className="tune-section">
          <h2>Parameter sweeps</h2>
          <p className="muted">
            What each parameter did to <code>{trials.data.primary_metric}</code>. One chart
            per parameter, because two of them on one plot would need two x-scales and the
            crossing point would be an artifact of the scales rather than a finding. The
            dashed line is what the region serves now.
          </p>
          <div className="sweeps">
            {Object.entries(trials.data.sweeps).map(([parameter, points]) => (
              <SweepChart
                key={parameter}
                parameter={parameter}
                stage={points[0]?.stage ?? ""}
                metric={trials.data!.primary_metric}
                points={points}
                baseline={
                  report.data?.baseline?.metrics?.[trials.data!.primary_metric] ?? null
                }
              />
            ))}
          </div>
        </section>
      ) : null}

      {lineage.data?.lineage?.length ? (
        <section className="tune-section">
          <h2>What this region has served</h2>
          <p className="muted">{lineage.data.base_explanation}</p>
          <ol className="lineage">
            {lineage.data.lineage.map((entry) => (
              <li key={`${entry.bundle_id}-${entry.applied_at}`}>
                <div className="lineage__head">
                  <code>{entry.bundle_id}</code>
                  {entry.active ? <span className="badge badge--live">serving</span> : null}
                  <span className="muted small">
                    applied {entry.applied_at} by {entry.applied_by}
                    {entry.superseded_at ? ` · replaced ${entry.superseded_at}` : ""}
                  </span>
                  {!entry.active ? (
                    <button
                      type="button"
                      className="btn"
                      onClick={() => rollback.mutate(entry.bundle_id)}
                      disabled={rollback.isPending}
                    >
                      Roll back to this
                    </button>
                  ) : null}
                </div>
                <div className="bundle__params">
                  {Object.entries(entry.parameters).map(([name, value]) => (
                    <span key={name}>
                      <code>{name}</code> = {value}
                    </span>
                  ))}
                </div>
              </li>
            ))}
          </ol>
        </section>
      ) : null}

      <section className="tune-section">
        <h2>Configuration bundles</h2>
        {bundles.data?.bundles?.length ? (
          <ul className="bundles">
            {bundles.data.bundles.map((bundle) => (
              <li key={bundle.bundle_id} className={bundle.active ? "bundle bundle--live" : "bundle"}>
                <div className="bundle__head">
                  <code>{bundle.bundle_id}</code>
                  {bundle.active ? <span className="badge badge--live">live</span> : null}
                  {!bundle.active ? (
                    <button
                      type="button"
                      className="btn btn--primary"
                      onClick={() => apply.mutate(bundle.bundle_id)}
                      disabled={apply.isPending}
                    >
                      Apply to the fleet
                    </button>
                  ) : null}
                </div>
                <div className="bundle__params">
                  {Object.entries(bundle.parameters).map(([name, value]) => (
                    <span key={name}>
                      <code>{name}</code> = {value}
                    </span>
                  ))}
                </div>
                {bundle.rationale ? <p className="muted">{bundle.rationale}</p> : null}
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted">
            No bundles yet. A batch produces one only when a parameter set beats the
            current configuration <em>and</em> confirms on a cohort it was never selected
            on.
          </p>
        )}
      </section>
    </div>
  );
}

/**
 * Pipeline behaviour on live traffic, between batches.
 *
 * Placed above the diagnosis on purpose. The diagnosis is a batch result and
 * can be days old; this is what the region is doing right now, and it is the
 * only thing on the page that can catch a regression the moment an applied
 * bundle starts serving.
 *
 * Every rate carries its denominator, and `insufficient_evidence` renders as
 * an absence rather than as zeroes: a 0% empty rate over four searches is not
 * good news, it is no news.
 */
function LiveHealth({
  health,
  explain,
}: {
  health?: TuningHealth;
  explain?: (term: string) => GlossaryEntry | undefined;
}) {
  if (!health) return null;
  if (health.status !== "measured") {
    return (
      <section className="tune-section">
        <h2>Live pipeline health</h2>
        <p className="muted">{health.reason}</p>
        {!health.observation_enabled ? (
          <p className="muted small">
            Set <code>observability.interactions.enabled</code> and{" "}
            <code>observability.interactions.stage_sample_rate</code> to collect it.
          </p>
        ) : null}
      </section>
    );
  }
  const empty = health.empty!;
  const arms = health.arms ?? {};
  return (
    <section className="tune-section">
      <h2>Live pipeline health</h2>
      <p className="muted">
        {health.samples} sampled searches. Says what the pipeline did — never whether an
        answer was correct. Its use is as a change detector.
      </p>
      {health.mixed_configurations ? (
        <p className="tune-warning">
          These samples span more than one configuration, so the rates below average
          across a change. Narrow the window to read either side.
        </p>
      ) : null}
      <div className="rate-tiles">
        <RateTile
          label="Returned nothing"
          value={empty.rate}
          denominator={`${empty.count} of ${health.samples} searches`}
          hint={
            Object.keys(empty.by_stage).length
              ? `mostly ${Object.keys(empty.by_stage)[0]}`
              : undefined
          }
          tone={empty.rate > 0.15 ? "bad" : "neutral"}
          entry={explain?.("empty_rate")}
        />
        {Object.entries(arms).map(([arm, stats]) => (
          <RateTile
            key={arm}
            label={`${arm} arm contributed`}
            value={stats.contribution_rate}
            denominator={`${stats.contributed} of ${stats.observed} searches`}
            hint={stats.failed ? `${stats.failed} failed` : undefined}
            tone={stats.failed ? "bad" : "neutral"}
            entry={explain?.(stats.failed ? "arm_failed" : "arm_contribution_rate")}
          />
        ))}
        <RateTile
          label="Truncated"
          value={health.truncation?.rate ?? null}
          denominator={`${health.truncation?.count ?? 0} of ${health.samples} searches`}
          hint="fused list was longer than what was returned"
          entry={explain?.("truncation_rate")}
        />
      </div>
    </section>
  );
}

/** A gate that failed but does not block is a warning, not a failure. */
function GateRow({ gate, entry }: { gate: TuningGate; entry?: GlossaryEntry }) {
  const state = gate.passed ? "pass" : gate.blocking ? "fail" : "warn";
  return (
    <li className={`gate gate--${state}`}>
      <span className={`badge badge--${state}`}>{state}</span>
      <strong>{gate.gate_id}</strong>
      <span className="muted">{gate.summary}</span>
      <Explain entry={entry} compact />
    </li>
  );
}
