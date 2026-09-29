import { useEffect, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import { useMutation } from "@tanstack/react-query";
import { api } from "../api/client";
import type { AssistantStatus, ChatAnswer } from "../api/types";
import { useSession } from "../state/session";
import { AnswerBody } from "./AnswerBody";
import { SourceStrip } from "./SourceStrip";

export type { ChatTurn } from "../state/session";

interface ChatPanelProps {
  status?: AssistantStatus;
  sessionId: string | null;
  hasContent: boolean;
  onCitationClick: (nodeId: string | undefined) => void;
  onConnectModel: () => void;
}

/** Workflow step names, said the way a person would say them. */
const STEP_LABELS: Record<string, string> = {
  plan: "Planning the search…",
  retrieve: "Searching your sources…",
  expand: "Following links in the graph…",
  grade: "Checking the evidence…",
  replan: "Evidence was thin — searching again…",
  synthesize: "Writing the answer…",
  verify: "Verifying citations…",
};

const SUGGESTIONS = [
  "What is this knowledge base about?",
  "Summarize the main themes across my sources.",
  "What decisions are recorded here, and why?",
];

export function ChatPanel({
  status,
  sessionId,
  hasContent,
  onCitationClick,
  onConnectModel,
}: ChatPanelProps) {
  // The conversation, the draft and the retrieval scope live in the session
  // store, so leaving for Sources or Settings and coming back finds the thread
  // exactly where it was — including a half-typed question.
  const { state, dispatch } = useSession();
  const { turns, draft, sourceFilter, sourceTypeFilter, workflow } = state;
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  // Keyed by turn id rather than a single ref: the effect below always needs
  // *the newest turn's* element, and turns re-render with new array
  // identities (new question appended, then the same turn updated in place
  // with its answer) rather than staying at a fixed index.
  const turnRefs = useRef(new Map<string, HTMLDivElement>());
  // Live workflow steps for the in-flight question. Local, not session state:
  // they describe one request and are meaningless once it resolves.
  const [progress, setProgress] = useState<{ name: string; detail: string }[]>([]);
  // Whether this region's remembered assertions may inform the answer. The
  // same `memory` field MCP and the router send, so the three surfaces cannot
  // disagree about what "off" means.
  const [useMemory, setUseMemory] = useState(true);

  const ask = useMutation({
    mutationFn: (question: string) => {
      setProgress([]);
      return api.chatStream(
        {
          question,
          session_id: sessionId,
          source_name: sourceFilter,
          source_types: sourceTypeFilter ? [sourceTypeFilter] : null,
          workflow,
          memory: useMemory ? null : "off",
        },
        (step) => setProgress((prev) => [...prev, { name: step.name, detail: step.detail }]),
      );
    },
    onSuccess: (answer, question) => {
      setProgress([]);
      dispatch({ type: "answered", question, answer });
    },
    onError: (error: Error, question) => {
      setProgress([]);
      dispatch({ type: "ask-failed", question, error: error.message });
    },
  });

  // Keep the newest turn's *question* pinned near the top of the viewport —
  // both when it's first asked and again once its answer lands. Scrolling to
  // the container's absolute bottom instead (the previous behavior) put the
  // viewport at the end of whatever answer just arrived, which for anything
  // longer than a screenful meant the question — and the start of the
  // answer — scrolled out of view above, forcing a manual scroll back up to
  // resume reading from where the turn began.
  useEffect(() => {
    const latest = turns[turns.length - 1];
    if (!latest) return;
    turnRefs.current.get(latest.id)?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [turns, ask.isPending]);

  const submit = (text: string) => {
    const question = text.trim();
    if (!question || ask.isPending) return;
    dispatch({ type: "ask", id: `${Date.now()}-${turns.length}`, question });
    if (textareaRef.current) textareaRef.current.style.height = "auto";
    ask.mutate(question);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submit(draft);
    }
  };

  const extractive = status ? !status.ready : false;

  return (
    <div className="chat">
      <div className="chat__scroll">
        {turns.length === 0 ? (
          <div className="chat-empty">
            <h2>Ask your knowledge base</h2>
            <p>
              {hasContent
                ? "Answers are grounded in your indexed sources and cite the passages they came from."
                : "Nothing is indexed yet. Add a source and it becomes answerable here."}
            </p>
            {hasContent ? (
              <div className="suggestions">
                {SUGGESTIONS.map((suggestion) => (
                  <button
                    key={suggestion}
                    className="suggestion"
                    onClick={() => submit(suggestion)}
                  >
                    {suggestion}
                  </button>
                ))}
              </div>
            ) : null}
          </div>
        ) : null}

        {turns.map((turn) => (
          <div
            key={turn.id}
            ref={(el) => {
              if (el) turnRefs.current.set(turn.id, el);
              else turnRefs.current.delete(turn.id);
            }}
          >
            <div className="msg msg--user">
              <div className="msg__bubble">{turn.question}</div>
            </div>
            {turn.error ? (
              <div className="msg">
                <div className="banner banner--error" style={{ marginBottom: 0 }}>
                  {turn.error}
                </div>
              </div>
            ) : turn.answer ? (
              <AnswerTurn answer={turn.answer} onCitationClick={onCitationClick} />
            ) : (
              <div className="msg">
                <span className="thinking">
                  <span className="spinner" />{" "}
                  {progress.length > 0
                    ? STEP_LABELS[progress[progress.length - 1].name] ??
                      progress[progress.length - 1].name
                    : workflow === "simple"
                      ? "Searching your sources…"
                      : "Planning the search…"}
                </span>
                {progress.length > 0 ? (
                  <ol className="progress">
                    {progress.map((step, index) => (
                      <li
                        key={`${step.name}-${index}`}
                        className={index === progress.length - 1 ? "progress__now" : undefined}
                      >
                        <span className="progress__name">
                          {STEP_LABELS[step.name] ?? step.name}
                        </span>
                        <span className="progress__detail">{step.detail}</span>
                      </li>
                    ))}
                  </ol>
                ) : null}
              </div>
            )}
          </div>
        ))}
      </div>

      <div className="chat__composer">
        <div className="composer">
          <textarea
            ref={textareaRef}
            rows={1}
            value={draft}
            placeholder={
              sourceFilter
                ? `Ask about ${sourceFilter}…`
                : sourceTypeFilter
                  ? `Ask about your ${sourceTypeFilter} sources…`
                  : "Ask anything about your sources…"
            }
            onChange={(event) => {
              dispatch({ type: "set-draft", text: event.target.value });
              const el = event.target;
              el.style.height = "auto";
              el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
            }}
            onKeyDown={onKeyDown}
            aria-label="Ask a question"
          />
          <button
            className="composer__send"
            onClick={() => submit(draft)}
            disabled={!draft.trim() || ask.isPending}
            aria-label="Send"
            title="Send (Enter)"
          >
            ↑
          </button>
        </div>
        <div className="composer__hint">
          {sourceFilter ? <span className="pill pill--accent">scoped to {sourceFilter}</span> : null}
          {sourceTypeFilter ? (
            <span className="pill pill--accent">only {sourceTypeFilter}</span>
          ) : null}
          <label className="composer__memory" title="Let remembered assertions inform the answer">
            <input
              type="checkbox"
              checked={useMemory}
              onChange={(event) => setUseMemory(event.target.checked)}
            />
            use memory
          </label>
          {extractive ? (
            <>
              <span>No model connected — answers are extracted passages, not synthesis.</span>
              <button className="btn btn--small" onClick={onConnectModel}>
                Connect a model
              </button>
            </>
          ) : status?.credential_source === "session" ? (
            <span>
              Using {status.session?.provider} · key held for this session only
            </span>
          ) : status?.credential_source === "environment" ? (
            <span>Using {status.configured_provider} from the server environment</span>
          ) : null}
        </div>
      </div>
    </div>
  );
}

/**
 * What the agent actually did.
 *
 * Collapsed by default — the answer is the product — but one click away,
 * because "it searched three times and walked the graph" is the difference
 * between trusting a slow answer and assuming it hung.
 */
function AgentTrace({ steps }: { steps: NonNullable<ChatAnswer["steps"]> }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="trace">
      <button className="trace__toggle" onClick={() => setOpen((v) => !v)}>
        {open ? "\u25be" : "\u25b8"} {steps.length} steps
      </button>
      {open ? (
        <ol className="trace__list">
          {steps.map((step, index) => (
            <li key={`${step.name}-${index}`}>
              <span className="trace__name">{step.name}</span>
              <span className="trace__detail">{step.detail}</span>
              <span className="trace__metrics">
                {step.passages > 0 ? (
                  <span>{step.passages} {step.name === "read" ? "files" : "passages"}</span>
                ) : null}
                <span>in {step.input_tokens == null ? "—" : step.input_tokens} tokens</span>
                <span>out {step.output_tokens == null ? "—" : step.output_tokens} tokens</span>
                <span>
                  {step.duration_seconds == null ? "—" : `${step.duration_seconds.toFixed(3)}s`}
                </span>
              </span>
            </li>
          ))}
        </ol>
      ) : null}
    </div>
  );
}

function AnswerTurn({
  answer,
  onCitationClick,
}: {
  answer: ChatAnswer;
  onCitationClick: (nodeId: string | undefined) => void;
}) {
  const byIndex = new Map(answer.citations.map((c) => [c.index, c]));
  return (
    <div className="msg">
      <div className="msg__answer">
        <AnswerBody
          text={answer.answer}
          onCite={(index) => onCitationClick(byIndex.get(index)?.node_id)}
        />
      </div>
      {answer.citations.length > 0 ? (
        <SourceStrip citations={answer.citations} onSelect={onCitationClick} />
      ) : null}
      {answer.steps && answer.steps.length > 1 ? <AgentTrace steps={answer.steps} /> : null}
      <div className="msg__meta">
        {answer.workflow ? <span className="pill">{answer.workflow}</span> : null}
        {answer.mode === "llm" ? (
          <span>
            {answer.provider}
            {answer.model ? ` · ${answer.model}` : ""}
          </span>
        ) : (
          <span>extracted passages</span>
        )}
        <span>·</span>
        <span>{answer.citations.length} sources</span>
        {answer.facts.length > 0 ? (
          <>
            <span>·</span>
            <span>{answer.facts.length} graph facts</span>
          </>
        ) : null}
        {answer.error ? <span className="error">· {answer.error}</span> : null}
      </div>
    </div>
  );
}
