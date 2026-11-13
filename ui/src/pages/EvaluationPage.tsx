import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import {
  GateRow,
  HealthTile,
  MetricDetail,
  Sparkline,
} from "../evaluation/primitives";
import type { EvaluationMetricResult, EvaluationStatus } from "../api/types";

const TREND_METRICS = [
  "known_positive_reciprocal_rank",
  "known_positive_recall_at_5",
  "negative_exposure_at_5",
  "query_evidence_coverage",
];

/** Phases a batch moves through, in order, so the reader knows where it is. */
const PHASES = [
  "snapshot",
  "cohorts",
  "proof",
  "variants",
  "replay",
  "metrics",
  "gates",
  "report",
  "persisting",
];

const TERMINAL = new Set(["completed", "truncated", "invalid", "failed", "interrupted"]);

/**
 * The order the health vector is meant to be read in.
 *
 * A stored report is JSON with sorted keys, so iterating the object renders it
 * alphabetically — which puts `control_regression` first and the coverage
 * caveat fifth. The vector is meant to be read top to bottom with its
 * denominator context before its scores, so the order is stated here rather
 * than inherited from a serializer. Anything not listed still renders, after.
 */
const VECTOR_ORDER = [
  "evidence_coverage",
  "known_positive_retrieval_at_5",
  "known_positive_reciprocal_rank",
  "known_negative_exposure_at_5",
  "memory_attributable_gain",
  "future_query_generalization",
  "generalization_gap",
  "control_regression",
];

function orderedVector(vector: Record<string, unknown>): string[] {
  const known = VECTOR_ORDER.filter((name) => name in vector);
  const rest = Object.keys(vector).filter((name) => !VECTOR_ORDER.includes(name));
  return [...known, ...rest];
}

/**
 * Is this knowledge base getting better, and how would we know?
 *
 * The page is organised around the answer being a *vector* rather than a
 * score. There is deliberately no headline percentage anywhere on it: the
 * tiles carry their denominators, a metric that could not be computed shows a
 * gap rather than a zero, and the hard gates sit apart from the numbers
 * because an ACL leak is not something good recall offsets.
 *
 * Progress comes from `/evaluation/status`, which reads `/state` rather than
 * the process running the batch. That is what makes this page work in the two
 * cases that actually happen: a browser talking to an API replica that did not
 * start the run, and a reader coming back after the container was restarted.
 * A batch whose heartbeat expired shows as *interrupted*, with how far it got
 * — never as a spinner nobody will ever stop.
 */
export function EvaluationPage() {
  const queryClient = useQueryClient();
  const [openMetric, setOpenMetric] = useState<string | null>(null);
  const [trendMetric, setTrendMetric] = useState(TREND_METRICS[0]);

  const status = useQuery({
    queryKey: ["evaluation", "status"],
    queryFn: () => api.evaluationStatus(),
    // Polls only while a batch is in flight. A page that polls forever is a
    // page that keeps a laptop fan on for a report nobody is watching change.
    refetchInterval: (query) => (query.state.data?.status === "running" ? 1500 : false),
    retry: false,
  });

  const report = useQuery({
    queryKey: ["evaluation", "report", status.data?.run_id],
    queryFn: () => api.evaluationReport(),
    retry: false,
    // Refetched when a run finishes, so the numbers on screen belong to the
    // batch whose progress bar just filled.
    enabled: status.data !== undefined && status.data.status !== "none",
  });

  const cohorts = useQuery({
    queryKey: ["evaluation", "cohorts"],
    queryFn: () => api.evaluationCohorts(),
    retry: false,
  });

  const trend = useQuery({
    queryKey: ["evaluation", "trend", trendMetric],
    queryFn: () => api.evaluationTrend({ metric: trendMetric, cohort: "anchor", variant: "B5" }),
    retry: false,
  });

  const start = useMutation({
    mutationFn: () => api.evaluationRun({ force: true }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["evaluation"] });
    },
  });

  const live = status.data;
  const finished = live && TERMINAL.has(live.status);
  const removeRun = useMutation({
    mutationFn: () => api.deleteEvaluationRun(live!.run_id!),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["evaluation"] }),
  });
  const data = report.data;

  // The metric id behind the open tile, which is not its display label.
  const openMetricId = openMetric ? data?.health_vector?.[openMetric]?.metric_id : undefined;

  // Fetched rather than dug out of the report. The report's sections carry the
  // attribution metrics only, and most of the health vector is not among them
  // — so reading the calculation out of the document worked for two tiles and
  // silently showed nothing for the rest. `/evaluation/metrics` exists for
  // exactly this: resolve an aggregate to the row that produced it.
  const detail = useQuery({
    queryKey: ["evaluation", "metric", openMetricId],
    // Deliberately not pinned to the anchor cohort. Three of the vector's
    // metrics are *about* another cohort by definition — control regression
    // lives on the control set, and the two generalization figures on the
    // learned and holdout sets — so filtering to the anchor returned nothing
    // for them and the tile silently showed no calculation.
    queryFn: () => api.evaluationMetrics({ metric: openMetricId }),
    enabled: Boolean(openMetricId),
    retry: false,
  });

  const openCalculation = useMemo<EvaluationMetricResult | null>(() => {
    // Aggregates have no query id; the per-query rows behind them share the
    // metric id and would otherwise be picked arbitrarily. Where several
    // cohorts computed the same metric, the anchor is the headline one.
    const rows = (detail.data?.results ?? []).filter((row) => row.query_id === null && row.result);
    const preferred = rows.find((row) => row.cohort_purpose === "anchor") ?? rows[0];
    return (preferred?.result as EvaluationMetricResult | undefined) ?? null;
  }, [detail.data]);

  if (status.isLoading) {
    return <div className="page"><p className="muted">Loading…</p></div>;
  }

  if (live && live.enabled === false && !finished) {
    return (
      <div className="page eval-page">
        <header className="page__header">
          <h1>Knowledge effectiveness</h1>
        </header>
        <section className="section eval-section">
          <h2 className="section__title">Evaluation is switched off</h2>
          <p>
            A batch replays recorded queries against a corpus-only baseline and the artifact
            system, then reports what changed with the evidence and the denominator attached.
            It is read-only: nothing it writes is ever indexed or retrievable as knowledge.
          </p>
          <p className="muted small">
            Turn it on with <code>evaluation.enabled</code> in Settings. It needs the
            interaction ledger (<code>observability.interactions.enabled</code>) to have
            recorded queries worth replaying — without it every demonstrated metric reports
            “not enough evidence” rather than a number.
          </p>
          <button data-permission="evaluation.manage" className="btn" onClick={() => start.mutate()} disabled={start.isPending}>
            Run one anyway
          </button>
        </section>
      </div>
    );
  }

  return (
    <div className="page eval-page">
      <header className="page__header">
        <h1>Knowledge effectiveness</h1>
        <div className="page__actions">
          {finished && (
            <button data-permission="evaluation.manage" className="btn" onClick={() => removeRun.mutate()} disabled={removeRun.isPending}
              title="Delete operational history; the published report remains in Artifacts">
              Delete run history
            </button>
          )}
          <button data-permission="evaluation.manage"
            className="btn btn--primary"
            onClick={() => start.mutate()}
            disabled={start.isPending || live?.status === "running"}
          >
            {live?.status === "running" ? "Running…" : "Run evaluation"}
          </button>
        </div>
      </header>

      <RunProgress status={live} />
      {removeRun.error ? <p role="alert">{String(removeRun.error)}</p> : null}

      {data ? (
        <>
          <section className="section eval-section">
            <h2 className="section__title">Health vector</h2>
            <p className="muted small">
              A vector, deliberately — there is no single “accuracy” score. Every tile carries
              its denominator, and a measurement that could not be made shows as a gap rather
              than as a zero.
            </p>
            <div className="eval-tiles">
              {orderedVector(data.health_vector).map((name) => (
                <button
                  key={name}
                  type="button"
                  className="eval-tiles__item"
                  onClick={() => setOpenMetric(openMetric === name ? null : name)}
                >
                  <HealthTile name={name} entry={data.health_vector[name]} />
                </button>
              ))}
            </div>
            {openMetric && openCalculation ? (
              <MetricDetail metric={openCalculation} />
            ) : openMetric && detail.isFetching ? (
              <p className="muted small">Loading the calculation…</p>
            ) : openMetric ? (
              <p className="muted small">
                No stored calculation for <code>{openMetricId ?? openMetric}</code> on the
                anchor cohort in this run.
              </p>
            ) : null}
          </section>

          <section className="section eval-section">
            <h2 className="section__title">Hard gates</h2>
            <p className="muted small">
              Evaluated <em>before</em> any score is combined. An ACL leak is not offset by
              good recall, and the arithmetic that would let it be is the arithmetic these sit
              outside of.
            </p>
            <ul className="eval-gates">
              {data.gates.map((gate) => (
                <GateRow key={gate.gate_id} gate={gate} />
              ))}
            </ul>
          </section>

          <section className="section eval-section">
            <h2 className="section__title">Evidence coverage</h2>
            <div className="eval-coverage">
              <p>
                <strong>{data.evidence_coverage.evidenced_queries}</strong> of{" "}
                <strong>{data.evidence_coverage.eligible_queries}</strong> eligible queries
                carried positive or negative outcome evidence, across{" "}
                {data.evidence_coverage.independent_interactions} independent interactions.
              </p>
              {data.evidence_coverage.reasons.length ? (
                <ul className="eval-reasons">
                  {data.evidence_coverage.reasons.map((reason) => (
                    <li key={reason}>{reason}</li>
                  ))}
                </ul>
              ) : (
                <p className="muted small">Every sufficiency condition is met.</p>
              )}
            </div>
            <p className="eval-explanation">{data.explanations.end_user}</p>
          </section>

          <section className="section eval-section">
            <h2 className="section__title">Learned versus later</h2>
            <p className="muted small">{data.generalization.note}</p>
            <div className="eval-split">
              <GeneralizationCell
                label="Recall of learned experience"
                metric={data.generalization.learned}
              />
              <GeneralizationCell
                label="Forward generalization"
                metric={data.generalization.temporal_holdout}
              />
              <GeneralizationCell label="Gap" metric={data.generalization.gap} />
            </div>
          </section>

          <section className="section eval-section">
            <h2 className="section__title">Trend</h2>
            <div className="eval-trend__controls">
              {TREND_METRICS.map((metric) => (
                <button
                  key={metric}
                  type="button"
                  className={`chip ${trendMetric === metric ? "chip--active" : ""}`}
                  onClick={() => setTrendMetric(metric)}
                >
                  {metric.replace(/_/g, " ")}
                </button>
              ))}
            </div>
            <p className="muted small">
              The anchor cohort, whose membership is frozen. A rolling trend would move for two
              reasons at once and could not separate “the region changed” from “the questions
              changed”.
            </p>
            {trend.data ? <Sparkline points={trend.data.points} /> : <p className="muted">…</p>}
          </section>

          {data.candidate_decisions.length ? (
            <section className="section eval-section">
              <h2 className="section__title">Candidate decisions</h2>
              <ul className="eval-candidates">
                {data.candidate_decisions.map((decision) => (
                  <li key={decision.candidate_id} className={`eval-candidate eval-candidate--${decision.decision}`}>
                    <div className="eval-candidate__head">
                      <code>{decision.candidate_id}</code>
                      <span className="eval-chip">{decision.decision.replace(/_/g, " ")}</span>
                      {decision.applied ? <span className="eval-chip eval-chip--pass">applied</span> : null}
                    </div>
                    <ul className="eval-reasons">
                      {decision.reasons.map((reason, index) => (
                        <li key={index}>{reason}</li>
                      ))}
                    </ul>
                    {decision.note ? <p className="muted small">{decision.note}</p> : null}
                  </li>
                ))}
              </ul>
            </section>
          ) : null}

          <section className="section eval-section">
            <h2 className="section__title">Cohorts</h2>
            <p className="muted small">
              Which questions produced these numbers. An empty cohort explains a “not enough
              evidence” far better than the metric can.
            </p>
            <table className="data-table">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Purpose</th>
                  <th className="num">Queries</th>
                  <th>Frozen</th>
                </tr>
              </thead>
              <tbody>
                {(cohorts.data?.cohorts ?? []).map((cohort) => (
                  <tr key={cohort.cohort_id}>
                    <td>{cohort.name}</td>
                    <td>{cohort.purpose.replace(/_/g, " ")}</td>
                    <td className="num">{cohort.query_count}</td>
                    <td>{cohort.frozen ? "yes" : "no"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>

          <section className="section eval-section">
            <h2 className="section__title">Limitations</h2>
            <ul className="eval-reasons">
              {data.limitations.unjudged_share !== null ? (
                <li>
                  {(data.limitations.unjudged_share * 100).toFixed(0)}% of the results in the
                  top five were never judged either way.
                </li>
              ) : null}
              {Object.keys(data.limitations.truncated_replays).length ? (
                <li>
                  {Object.keys(data.limitations.truncated_replays).length} cohort/variant pairs
                  were not replayed within this run’s budget.
                </li>
              ) : null}
              {data.limitations.metrics_withheld.map((withheld) => (
                <li key={withheld.metric_id}>
                  <code>{withheld.metric_id}</code> was withheld: {withheld.problems.join(", ")}.
                </li>
              ))}
              {data.snapshot_integrity.incomplete_sections.length ? (
                <li>
                  Snapshot sections that could not be resolved:{" "}
                  {data.snapshot_integrity.incomplete_sections.join(", ")}.
                </li>
              ) : null}
            </ul>
          </section>
        </>
      ) : report.isError ? (
        <section className="section eval-section">
          <h2 className="section__title">No report yet</h2>
          <p className="muted">
            Nothing has been evaluated for this knowledge base. Run a batch to produce one.
          </p>
        </section>
      ) : null}
    </div>
  );
}

function GeneralizationCell({
  label,
  metric,
}: {
  label: string;
  metric: EvaluationMetricResult | null;
}) {
  return (
    <div className="eval-split__cell">
      <div className="eval-split__label">{label}</div>
      <div className="eval-split__value">
        {metric?.result.value === null || metric?.result.value === undefined
          ? "—"
          : metric.result.value >= 0
            ? `+${metric.result.value.toFixed(4)}`
            : metric.result.value.toFixed(4)}
      </div>
      <div className="muted small">
        {metric
          ? metric.result.denominator
            ? `over ${metric.result.denominator} queries`
            : metric.result.status.replace(/_/g, " ")
          : "not computed"}
      </div>
    </div>
  );
}

/**
 * The live position of a batch.
 *
 * Renders four distinct states, and the distinctions are the point: running
 * (with a phase and a filled bar), interrupted (a container stopped — how far
 * it got, and that re-running resumes it), failed (with the reason), and
 * finished. A resumed attempt says so, because "attempt 2" is the difference
 * between a slow run and a run that has already been killed once.
 */
function RunProgress({ status }: { status: EvaluationStatus | undefined }) {
  if (!status || status.status === "none") {
    return (
      <section className="section eval-progress">
        <p className="muted">
          No batch has run for this knowledge base yet.
        </p>
      </section>
    );
  }
  const running = status.status === "running";
  const fraction = status.fraction ?? 0;
  const phaseIndex = PHASES.indexOf(String(status.phase ?? ""));
  return (
    <section className={`card eval-progress eval-progress--${status.status}`}>
      <div className="eval-progress__head">
        <span className={`eval-chip eval-chip--${status.status}`}>{status.status}</span>
        <code className="eval-progress__run">{status.run_id}</code>
        {status.mode ? <span className="muted small">{status.mode.replace(/_/g, " ")}</span> : null}
        {status.owner ? <span className="muted small">on {status.owner}</span> : null}
      </div>

      {running ? (
        <>
          <div className="eval-progress__bar" role="progressbar" aria-valuenow={Math.round(fraction * 100)} aria-valuemin={0} aria-valuemax={100}>
            <div className="eval-progress__fill" style={{ width: `${Math.round(fraction * 100)}%` }} />
          </div>
          <ol className="eval-phases">
            {PHASES.map((phase, index) => (
              <li
                key={phase}
                className={
                  index < phaseIndex
                    ? "eval-phase eval-phase--done"
                    : index === phaseIndex
                      ? "eval-phase eval-phase--current"
                      : "eval-phase"
                }
              >
                {phase}
              </li>
            ))}
          </ol>
          <p className="muted small">
            {status.phase_detail ?? status.phase} — {status.completed_units ?? 0} of{" "}
            {status.total_units ?? 0} cohort/variant replays
          </p>
        </>
      ) : null}

      {status.status === "interrupted" ? (
        <p className="eval-progress__note">
          The process running this batch stopped before it finished — it got{" "}
          {status.completed_units ?? 0} of {status.total_units ?? 0} replays in. Running it
          again resumes from there rather than starting over.
        </p>
      ) : null}

      {status.status === "failed" && status.error ? (
        <p className="eval-progress__note">
          Failed: <code>{status.error}</code>. The finished replays are kept, so re-running
          picks up where this attempt stopped.
        </p>
      ) : null}

      {(status.attempts ?? 1) > 1 ? (
        <p className="muted small">
          Attempt {status.attempts}: an earlier attempt was interrupted and this one resumed it.
        </p>
      ) : null}
    </section>
  );
}
