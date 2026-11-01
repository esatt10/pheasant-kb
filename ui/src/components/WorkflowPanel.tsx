import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";

/**
 * Which agent workflow answers questions, and how it is tuned.
 *
 * The picker is the low-code half; the options editor underneath is the
 * developer half — the same `assistant.workflow_options` keys the YAML and
 * the `POST /assistant/chat` body accept, with the defaults shown inline so
 * there is nothing to look up.
 */
export function WorkflowPanel({
  selected,
  onSelect,
}: {
  selected: string | null;
  onSelect: (name: string | null) => void;
}) {
  const catalog = useQuery({ queryKey: ["workflows"], queryFn: api.workflows });
  const [showOptions, setShowOptions] = useState(false);

  if (catalog.isLoading) {
    return (
      <p className="muted small">
        <span className="spinner" /> Loading…
      </p>
    );
  }
  if (catalog.isError) {
    return <div className="banner banner--error">{(catalog.error as Error).message}</div>;
  }
  const data = catalog.data!;
  const effective = selected ?? data.active;
  const agenticDefaults = data.option_defaults?.agentic ?? {};

  return (
    <div className="form-grid">
      <div className="field">
        <span>Workflow</span>
        <div className="radio-cards">
          <button
            className={`radio-card${selected === null ? " active" : ""}`}
            onClick={() => onSelect(null)}
          >
            <span className="radio-card__title">Automatic</span>
            <span className="radio-card__sub">
              Configured: <code>{data.configured}</code> → currently runs{" "}
              <code>{data.active}</code>
            </span>
          </button>
          {data.workflows.map((workflow) => (
            <button
              key={workflow.name}
              className={`radio-card${selected === workflow.name ? " active" : ""}`}
              onClick={() => onSelect(workflow.name)}
              title={workflow.description}
            >
              <span className="radio-card__title">
                {workflow.label}
                {workflow.builtin ? "" : " ·  plugin"}
              </span>
              <span className="radio-card__sub">{workflow.description}</span>
            </button>
          ))}
        </div>
      </div>

      {!data.agent_extra_installed ? (
        <div className="banner banner--warn" style={{ marginBottom: 0 }}>
          The agentic workflow needs the agent extra:{" "}
          <code>pip install 'pheasant-kb[agent]'</code>. Until then every question runs the
          single-pass workflow.
        </div>
      ) : null}

      <p className="muted small" style={{ margin: 0 }}>
        <strong>Agentic</strong> reads each question for itself: “what does this repo
        do?” is answered as a <em>knowledge summary</em> (more files, less of each),
        “how do I use this tool?” as <em>procedural steps</em> with real code from the
        index (fewer files, read further). The chat trace shows which way it went. To
        pin the other direction, set <code>{'workflow_options: {intent: procedural}'}</code>.
      </p>

      <p className="muted small" style={{ margin: 0 }}>
        Selected here it applies to your next question only. To make it the default set{" "}
        <code>assistant.workflow: {effective}</code> in Settings → Raw YAML. To ship your
        own, register it under the <code>pheasant.agent_workflows</code> entry-point group.
      </p>

      <button className="btn btn--small btn--ghost" onClick={() => setShowOptions((v) => !v)}>
        {showOptions ? "Hide" : "Show"} tuning options
      </button>

      {showOptions ? (
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>Option</th>
                <th>Default</th>
                <th>Effect</th>
              </tr>
            </thead>
            <tbody>
              {OPTION_HELP.map(([key, help]) => (
                <tr key={key}>
                  <td>
                    <code>{key}</code>
                  </td>
                  <td className="muted small">
                    {JSON.stringify((agenticDefaults as Record<string, unknown>)[key])}
                  </td>
                  <td className="muted small">{help}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </div>
  );
}

const OPTION_HELP: [string, string][] = [
  [
    "intent",
    "Answer shape: auto reads it off the question, or pin knowledge / procedural / compose. " +
      "Also sets retrieval breadth-vs-depth and the sufficiency bar.",
  ],
  ["max_rounds", "How many plan → retrieve → grade loops before answering with what it has."],
  ["retrieval_modes", "Search modes to fan out over. Unavailable modes are dropped."],
  ["expand_graph", "Walk the knowledge graph out of the best hits for related material."],
  ["expand_depth", "How many hops the graph walk takes."],
  ["expand_per_node", "Related documents pulled in per hit."],
  ["per_query_results", "Passages fetched per query per mode."],
  ["max_context_passages", "Passages offered to the answering step."],
  ["grade_evidence", "Ask the model whether its evidence is sufficient before answering."],
  ["verify_citations", "Drop [n] markers that do not resolve to a real passage."],
  ["max_facts", "Graph facts surfaced alongside the answer."],
  [
    "include_full_content",
    "Read the whole cited file, rebuilt from its chunks, instead of the 500-character " +
      "search preview. Turning this off makes answers noticeably thinner.",
  ],
  ["passage_chars", "Prose allowance per file."],
  [
    "code_passage_chars",
    "Ceiling for code and config, which are never excerpted — a module missing its " +
      "imports is what makes a model invent one.",
  ],
  [
    "large_file_bytes",
    "Original file size above which prose is cut to the matched chunks and their " +
      "neighbours rather than read whole.",
  ],
  ["context_budget_chars", "Total characters of file content across all passages."],
];
