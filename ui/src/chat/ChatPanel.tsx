import { recordUI } from "../telemetry";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, collectAnswer } from "../api/client";
import type {
  AnswerDepth,
  AssistantStatus,
  ChatAnswer,
  Citation,
  VisualResponse,
  WorkflowStep,
} from "../api/types";
import { useArchiveRefresh } from "../browser/archive";
import { browsedFrom, excludedInScope, historyFor, useSession } from "../state/session";
import { AnswerScope } from "./AnswerScope";
import { AnswerBody } from "./AnswerBody";
import { InventoryListing, listingText } from "./InventoryListing";
import {
  KeywordReference,
  insertKeyword,
  keywordMatches,
  useKeywordReference,
} from "./KeywordReference";
import { ArtifactCard, showArtifacts } from "../artifacts/ArtifactsPanel";
import { McpAppFrame } from "./McpAppFrame";
import { forgetPending, newAnswerId, pendingAnswer, rememberPending } from "./pendingAnswer";
import { MicButton, useDictation } from "./MicButton";
import { dictationUnsupported, spliceAtCaret } from "./dictation";
import { SourceStrip, openable } from "./SourceStrip";

export type { ChatTurn } from "../state/session";

interface ChatPanelProps {
  status?: AssistantStatus;
  sessionId: string | null;
  hasContent: boolean;
  /** False until the knowledge base has answered once (it may be waking). */
  contentKnown?: boolean;
  onCitationClick: (nodeId: string | undefined) => void;
  onConnectModel: () => void;
}

/** Workflow step names, said the way a person would say them. */
const STEP_LABELS: Record<string, string> = {
  plan: "Planning the search…",
  retrieve: "Searching your sources…",
  hybrid: "Broadening the search with hybrid retrieval…",
  expand: "Following links in the graph…",
  grade: "Checking the evidence…",
  decide: "Judging the evidence…",
  replan: "Evidence was thin — searching again…",
  prune: "Setting aside passages that do not bear on it…",
  synthesize: "Writing the answer…",
  verify: "Verifying citations…",
  accept: "Checking the answer against its sources…",
  context: "Reading the question in context…",
  classify: "Reading the question…",
  inventory: "Listing from the index…",
  search: "Ranking the search hits…",
  outline: "Outlining a long answer…",
  sections: "Writing the sections…",
  visual: "Drawing the visual…",
};

const DEPTHS: { value: AnswerDepth | "auto"; label: string; title: string }[] = [
  { value: "auto", label: "Auto length", title: "Let each question decide how long its answer is" },
  { value: "short", label: "Short", title: "A direct answer" },
  { value: "medium", label: "Medium", title: "An overview in a few sections" },
  { value: "long", label: "Long", title: "An outlined, sectioned write-up (slower)" },
];

const SUGGESTIONS = [
  "What is this knowledge base about?",
  "Summarize the main themes across my sources.",
  "What decisions are recorded here, and why?",
  // Questions about the knowledge base itself are answered from the index.
  "@pheasant what's in this knowledge base?",
  "@pheasant how are my sources linked?",
];

export function ChatPanel({
  status,
  sessionId,
  hasContent,
  contentKnown = true,
  onCitationClick,
  onConnectModel,
}: ChatPanelProps) {
  // The conversation, the draft and the retrieval scope live in the session
  // store, so leaving for Sources or Settings and coming back finds the thread
  // exactly where it was — including a half-typed question.
  const { state, dispatch } = useSession();
  const { turns, draft, sourceFilter, sourceTypeFilter, workflow, answerDepth, pathScope, browser } = state;
  // Several sources selected in the rail: answered from all of them. A folder
  // scope is always inside one source, so it never travels with a list.
  const scopedSources =
    !pathScope && sourceFilter && state.alsoSources.length ? [sourceFilter, ...state.alsoSources] : null;
  const omitted = excludedInScope(state.excluded, pathScope?.source ?? sourceFilter, pathScope);
  // The earlier turns a question is sent with. The region keeps no chat
  // state, so this is what makes "and what about the second one?" answerable.
  const history = historyFor(state);
  const queryClient = useQueryClient();
  // The answered turns of this topic: what Save thread keeps, as one artifact.
  const topicTurns = state.turns.slice(state.contextFrom).filter((turn) => turn.answer);
  const saveThread = useMutation({
    mutationFn: () =>
      api.saveArtifact({
        kind: "thread",
        conversation_id: state.conversationId,
        turns: topicTurns.map((turn) => ({
          question: turn.question,
          answer: turn.answer,
          answer_id: turn.answerId,
          // A diagram drawn or redrawn for the turn is the one kept.
          visual: turn.drawn?.visual?.status === "ok" ? turn.drawn.visual : undefined,
        })),
      }),
    onSuccess: (saved) => {
      queryClient.invalidateQueries({ queryKey: ["artifacts"] });
      dispatch({ type: "open-artifact", id: saved.id });
      showArtifacts();
    },
  });
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const chatRef = useRef<HTMLDivElement>(null);
  // The @-keyword quick reference: a floating card beside the chat column.
  const reference = useKeywordReference();
  const keywords = status?.keywords ?? [];
  // Keyed by turn id rather than a single ref: the effect below always needs
  // *the newest turn's* element, and turns re-render with new array
  // identities (new question appended, then the same turn updated in place
  // with its answer) rather than staying at a fixed index.
  const turnRefs = useRef(new Map<string, HTMLDivElement>());
  // Live workflow steps for the in-flight question. Local, not session state:
  // they describe one request and are meaningless once it resolves.
  const [progress, setProgress] = useState<WorkflowStep[]>([]);
  const [answerPreview, setAnswerPreview] = useState("");
  // Whether this region's remembered assertions may inform the answer. The
  // same `memory` field MCP and the router send, so the three surfaces cannot
  // disagree about what "off" means.
  // Off by default: an answer is grounded in the sources unless somebody asks
  // for remembered assertions too.
  // Whether saved artifacts (composed answers and reports) may be evidence;
  // the `artifacts` field search and answers take on every surface.
  const [useArtifacts, setUseArtifacts] = useState(true);
  // `@remove` changed the index: the taxonomy view's listings are stale.
  const refreshArchive = useArchiveRefresh();

  // Dictation: transcript text is written wherever the caret is, as it
  // arrives. `draftRef` is the text as of the last write (several deltas can
  // land before React renders), and `caretRef` the caret that write left,
  // restored after the render that moves a controlled textarea's caret to
  // its end.
  const voice = useQuery({
    queryKey: ["assistant-voice", sessionId],
    queryFn: () => api.voiceStatus(sessionId),
    staleTime: 60_000,
  });
  const draftRef = useRef(draft);
  draftRef.current = draft;
  const caretRef = useRef<number | null>(null);
  const writeDictated = (text: string, separate: boolean) => {
    const el = textareaRef.current;
    const value = draftRef.current;
    const start = caretRef.current ?? el?.selectionStart ?? value.length;
    const end = caretRef.current ?? el?.selectionEnd ?? value.length;
    const next = spliceAtCaret(value, start, end, text, separate);
    draftRef.current = next.value;
    caretRef.current = next.caret;
    dispatch({ type: "set-draft", text: next.value });
  };
  useLayoutEffect(() => {
    const el = textareaRef.current;
    if (!el || caretRef.current === null) return;
    el.setSelectionRange(caretRef.current, caretRef.current);
    caretRef.current = null;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
  }, [draft]);
  const dictation = useDictation({
    sessionId,
    maxSeconds: voice.data?.max_seconds ?? 60,
    onText: writeDictated,
    onLive: () => textareaRef.current?.focus(),
  });
  const micUnavailable = voice.data
    ? voice.data.available
      ? dictationUnsupported()
      : voice.data.reason || "not configured on the server"
    : "checking…";

  const ask = useMutation({
    mutationFn: ({
      id,
      question,
      answerId,
      resume = false,
    }: {
      id: string;
      question: string;
      answerId: string;
      /** Collect an answer a previous page load asked for, rather than ask. */
      resume?: boolean;
    }) => {
      setProgress([]);
      setAnswerPreview("");
      // The text lands as soon as it exists; a visual follows and replaces
      // the pending placeholder in the same turn.
      const onAnswer = (answer: ChatAnswer) => {
        setProgress([]);
        setAnswerPreview("");
        dispatch({ type: "answered", id, answer });
      };
      const onStep = (step: WorkflowStep) => setProgress((prev) => [...prev, step]);
      if (resume) return collectAnswer(answerId, { onStep, onAnswer });
      return api.chatStream(
        {
          question,
          // The region keeps the answer under this, so a dropped stream (a
          // phone that slept) collects it instead of failing.
          answer_id: answerId,
          session_id: sessionId,
          // A folder or file scope names its own source.
          source_name: scopedSources ? null : (pathScope?.source ?? sourceFilter),
          source_names: scopedSources,
          path_prefix: pathScope?.path ?? null,
          exclude_documents: omitted.length ? omitted : null,
          // What `@current` and `@dir` mean: whatever the taxonomy view shows.
          current_document: browser.open && browser.file ? `${browser.file.source}/${browser.file.path}` : null,
          current_directory:
            browser.open && browser.source ? (browser.path ? `${browser.source}/${browser.path}` : browser.source) : null,
          source_types: sourceTypeFilter ? [sourceTypeFilter] : null,
          workflow,
          artifact_policy: null,
          conversation_id: state.conversationId,
          artifacts: useArtifacts ? null : "exclude",
          history,
          depth: answerDepth,
        },
        onStep,
        undefined,
        onAnswer,
        (delta) => setAnswerPreview((previous) => previous + delta),
      );
    },
    onSuccess: (answer, { id }) => {
      forgetPending(id);
      setProgress([]);
      setAnswerPreview("");
      dispatch({ type: "answered", id, answer });
      if (answer.archive) refreshArchive();
    },
    onError: (error: Error, { id }) => {
      forgetPending(id);
      setProgress([]);
      setAnswerPreview("");
      dispatch({ type: "ask-failed", id, error: error.message });
    },
  });

  // A question the previous page load was still waiting on -- a phone
  // browser reloads a tab it discarded in the background -- is shown again
  // and its answer collected from the region, which went on writing it.
  const resumed = useRef(false);
  useEffect(() => {
    if (resumed.current) return;
    resumed.current = true;
    const pending = pendingAnswer();
    if (!pending || turns.some((turn) => turn.id === pending.id)) return;
    dispatch({ type: "ask", id: pending.id, question: pending.question, answerId: pending.answerId });
    ask.mutate({ ...pending, resume: true });
    // Once, on mount: `turns` and `ask` are read as they are then.
  }, []);

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
    recordUI("ask", window.location.pathname);
    // `@taxonomy` on its own opens the browser at once, on the source the chat
    // is scoped to; the answer (from the index, no model) confirms where.
    if (/^@(taxonomy|browse|folders)$/i.test(question)) {
      dispatch({ type: "open-browser", source: sourceFilter, path: "" });
    }
    // A sent question ends a dictation; its last words must not start the next one.
    dictation.cancel();
    const id = `${Date.now()}-${turns.length}`;
    const answerId = newAnswerId();
    rememberPending({ id, question, answerId, askedAt: Date.now() });
    dispatch({ type: "ask", id, question, answerId });
    if (textareaRef.current) textareaRef.current.style.height = "auto";
    ask.mutate({ id, question, answerId });
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    // Tab completes a keyword being typed as the first word.
    if (event.key === "/" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      reference.toggle();
      return;
    }
    const completions = event.key === "Tab" ? keywordMatches(draft, keywords) : [];
    if (completions.length > 0) {
      event.preventDefault();
      dispatch({ type: "set-draft", text: `${completions[0].keyword} ` });
      return;
    }
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submit(draft);
    }
  };

  const extractive = status ? !status.ready : false;

  return (
    <div className="chat" ref={chatRef}>
      <div className="chat__scroll">
        {turns.length === 0 ? (
          <div className="chat-empty">
            <h2>Ask your knowledge base</h2>
            <p>
              {!contentKnown
                ? "Waiting for the knowledge base to answer. If it was idle it is waking up; the banner above says what is still starting."
                : hasContent
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
              <AnswerTurn
                answer={turn.answer}
                drawn={turn.drawn ?? null}
                onDrawn={(drawn) => dispatch({ type: "visual-drawn", id: turn.id, drawn })}
                onCitationClick={onCitationClick}
                onAsk={submit}
                sessionId={sessionId}
                sourceName={sourceFilter}
                answerId={turn.answerId ?? null}
              />
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
                {answerPreview ? (
                  <div className="msg__bubble" style={{ whiteSpace: "pre-wrap", marginTop: 12 }}>
                    <small>Draft answer · checking citations</small>
                    <div>{answerPreview}</div>
                  </div>
                ) : null}
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
        <KeywordReference
          keywords={keywords}
          draft={draft}
          pinned={reference.open}
          anchorRef={chatRef}
          onClose={() => {
            reference.setOpen(false);
            textareaRef.current?.focus();
          }}
          onInsert={(text) => {
            dispatch({ type: "set-draft", text: insertKeyword(draft, text) });
            textareaRef.current?.focus();
          }}
        />
        <div className="composer">
          <textarea
            ref={textareaRef}
            rows={1}
            value={draft}
            placeholder={
              pathScope
                ? `Ask about ${pathScope.source}/${pathScope.path}…`
                : scopedSources
                ? `Ask about ${scopedSources.join(", ")}…`
                : sourceFilter
                ? `Ask about ${sourceFilter}…`
                : sourceTypeFilter
                  ? `Ask about your ${sourceTypeFilter} sources…`
                  : "Ask anything — or start with @ for @pheasant, @table, @doc, @search…"
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
          {voice.data?.available || dictation.state !== "off" ? (
            <MicButton control={dictation} unavailable={dictation.state === "off" ? micUnavailable : null} />
          ) : null}
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
          {dictation.state === "live" ? (
            <span className="composer__note composer__mic-note" role="status">
              Listening — words go where the cursor is
            </span>
          ) : dictation.notice ? (
            <span className="composer__note composer__mic-note" role="status">
              {dictation.notice}
            </span>
          ) : null}
          {keywords.length > 0 ? (
            <button
              type="button"
              className={`btn btn--small${reference.open ? " btn--active" : ""}`}
              aria-pressed={reference.open}
              onClick={reference.toggle}
              title="Keyword quick reference (Ctrl+/)"
            >
              @ keywords
            </button>
          ) : null}
          {pathScope ? (
            <span
              className="pill pill--accent scope-pill"
              title={`Questions are answered only from ${pathScope.source}/${pathScope.path}`}
            >
              only {pathScope.kind === "file" ? "from" : "in"} {pathScope.source}/{pathScope.path}
              <button
                type="button"
                className="scope-pill__clear"
                onClick={() => dispatch({ type: "set-path-scope", scope: null })}
                aria-label="Stop searching only here"
                title={`Search all of ${pathScope.source} again`}
              >
                ✕
              </button>
            </span>
          ) : scopedSources ? (
            <span className="pill pill--accent" title="Answered from every selected source">
              scoped to {scopedSources.length} sources: {scopedSources.join(", ")}
            </span>
          ) : sourceFilter ? (
            <span className="pill pill--accent">scoped to {sourceFilter}</span>
          ) : null}
          {sourceTypeFilter ? (
            <span className="pill pill--accent">only {sourceTypeFilter}</span>
          ) : null}
          {omitted.length ? (
            <span className="pill scope-pill" title={`Left out of every answer:\n${omitted.join("\n")}`}>
              omitting {omitted.length === 1 ? omitted[0].split("/").pop() : `${omitted.length} documents`}
              <button
                type="button"
                className="scope-pill__clear"
                onClick={() => dispatch({ type: "clear-excluded", source: pathScope?.source ?? sourceFilter })}
                aria-label="Stop omitting documents"
                title="Search the omitted documents again"
              >
                ✕
              </button>
            </span>
          ) : null}
          <select
            className="composer__depth"
            value={answerDepth ?? "auto"}
            title={DEPTHS.find((d) => d.value === (answerDepth ?? "auto"))?.title}
            onChange={(event) =>
              dispatch({
                type: "set-answer-depth",
                depth: event.target.value === "auto" ? null : (event.target.value as AnswerDepth),
              })
            }
            aria-label="Answer length"
          >
            {DEPTHS.map((depth) => (
              <option key={depth.value} value={depth.value} title={depth.title}>
                {depth.label}
              </option>
            ))}
          </select>
          {topicTurns.length > 0 ? (
            <button data-permission="artifacts.manage"
              type="button"
              className="btn btn--small"
              onClick={() => saveThread.mutate()}
              disabled={saveThread.isPending}
              title="Keep this conversation -- every question, answer, source and trace -- as one artifact"
            >
              {saveThread.isPending ? "Saving…" : saveThread.isSuccess ? "Thread saved" : "Save thread"}
            </button>
          ) : null}
          {saveThread.isError ? <span className="error small">{(saveThread.error as Error).message}</span> : null}
          {history.length > 0 ? (
            <span className="composer__context" title="Earlier turns sent with your next question">
              continuing from {history.length} earlier turn{history.length === 1 ? "" : "s"}
              <button
                type="button"
                className="btn btn--small"
                onClick={() => dispatch({ type: "new-topic" })}
                title="Answer the next question on its own; the thread stays visible"
              >
                New topic
              </button>
            </span>
          ) : null}
          <label className="composer__memory" title="Let knowledge records, saved reports and composed work inform the answer">
            <input
              type="checkbox"
              checked={useArtifacts}
              onChange={(event) => setUseArtifacts(event.target.checked)}
            />
            use artifacts
          </label>
          {extractive ? (
            <>
              <span className="composer__note">No model connected — answers are extracted passages, not synthesis.</span>
              <button
                className="btn btn--small"
                onClick={onConnectModel}
                title="No model connected — answers are extracted passages, not synthesis."
              >
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
              <div className="trace__detail">
                <div>
                  {step.detail}
                  {step.model ? (
                    <span className="trace__model" title="Model this step used">
                      {step.model}
                    </span>
                  ) : null}
                </div>
                <FanoutBreakdown timings={step.fanout_timings} />
              </div>
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

function FanoutBreakdown({ timings }: { timings?: WorkflowStep["fanout_timings"] }) {
  if (!timings?.length) return null;
  return (
    <div className="trace__fanouts">
      {timings.map((timing, index) => {
        const label =
          timing.phase === "embedding"
            ? `${timing.mode} embeddings · ${timing.query_count ?? 0} queries`
            : `${timing.mode} · ${timing.query_label ?? `query ${(timing.query_index ?? 0) + 1}`}`;
        return (
          <span className="trace__fanout" key={`${timing.mode}-${timing.phase}-${index}`}>
            <span>{label}</span>
            {timing.passages == null ? null : <span>{timing.passages} passages</span>}
            <span className="trace__fanout-time">{timing.duration_seconds.toFixed(3)}s</span>
          </span>
        );
      })}
    </div>
  );
}

function AnswerTurn({
  answer,
  drawn,
  onDrawn,
  onCitationClick,
  onAsk,
  sessionId,
  sourceName,
  answerId,
}: {
  answer: ChatAnswer;
  /** The visual drawn or redrawn for this turn, held in the session. */
  drawn: VisualResponse | null;
  onDrawn: (drawn: VisualResponse) => void;
  onCitationClick: (nodeId: string | undefined) => void;
  onAsk: (text: string) => void;
  sessionId: string | null;
  sourceName: string | null;
  /** What the region keeps this answer under, so it can be saved as a report. */
  answerId: string | null;
}) {
  const byIndex = new Map(answer.citations.map((c) => [c.index, c]));
  const queryClient = useQueryClient();
  const composed = answer.route?.intent === "compose";
  // An answer worth keeping becomes a report the region holds until deleted,
  // saved from the copy it kept of this very answer (`answer_resume`).
  const saveReport = useMutation({
    mutationFn: () =>
      api.saveArtifact({
        answer_id: answerId ?? undefined,
        conversation_id: state.conversationId,
        // Redrawn after the answer: keep the diagram as it is shown now.
        ...(drawn?.visual?.status === "ok" ? { visual: drawn.visual, citations: drawn.citations } : {}),
      }),
    onSuccess: (saved) => {
      queryClient.invalidateQueries({ queryKey: ["artifacts"] });
      dispatch({ type: "open-artifact", id: saved.id });
      showArtifacts();
    },
  });
  // A visual asked for after the fact — of the whole answer, or of one
  // passage — or redrawn in the view. Held on the turn in the session rather
  // than here, so it outlives this component: a re-render, a new question or
  // a remount must not bring the original picture back.
  const draw = useMutation({
    mutationFn: (citations: Citation[]) =>
      api.visualize({
        request: answer.question,
        node_ids: citations
          .map((citation) => citation.chunk_id ?? citation.node_id)
          .filter((id): id is string => Boolean(id))
          .slice(0, 12),
        session_id: sessionId,
        source_name: sourceName,
      }),
    onSuccess: onDrawn,
  });
  const cited = answer.citations.filter((citation) => citation.used);
  const chosen = drawn?.visual ?? answer.visual ?? null;
  // An image gallery repeats nothing the answer already shows inline: the
  // figures the text names are drawn in place, so only the rest belong here.
  const visual =
    chosen?.type === "images" && (chosen.figures ?? []).every((figure) => figure.shown)
      ? null
      : chosen;
  const visualCitations = drawn ? drawn.citations : answer.citations;
  // One object per change, not per render: the frame re-sends its result to
  // the view whenever this identity changes, and a fresh literal every render
  // sent the original visual back over a redraw on any re-render at all.
  const figures = answer.figures;
  const frameResult = useMemo(
    () => ({ visual, citations: visualCitations, figures: figures ?? [] }),
    [visual, visualCitations, figures],
  );
  // A paged listing with rows is drawn as a table that grows in place; an
  // empty one is just its sentence.
  const { state, dispatch } = useSession();
  const browsed = browsedFrom(answer);
  // A folder listing is its own view (the browser beside the chat), so it is
  // never drawn as the paged documents table.
  const listing =
    answer.inventory?.page && answer.inventory.page.total > 0 && !browsed
      ? listingText(answer.answer)
      : null;
  const openFile = (file: { source: string; path: string; chunkId?: string | null }) =>
    dispatch({ type: "open-file", file });
  /**
   * Selecting a citation aims the graph at it and, while the source browser
   * is open, opens the cited document there at the passage — which the
   * viewer highlights in whichever rendering is showing.
   */
  const selectCitation = (citation: Citation | undefined) => {
    onCitationClick(citation?.node_id);
    if (citation && state.browser.open && openable(citation)) {
      openFile({
        source: citation.source_id as string,
        path: citation.relative_path as string,
        chunkId: citation.chunk_id ?? null,
      });
    }
  };
  const onCite = (index: number) => selectCitation(byIndex.get(index));
  return (
    <div className="msg">
      <div className="msg__answer">
        {listing && answer.inventory ? (
          <>
            {listing.body ? <AnswerBody text={listing.body} onCite={onCite} /> : null}
            <InventoryListing
              inventory={answer.inventory}
              lead={listing.lead}
              onSelect={onCitationClick}
              onOpen={openFile}
            />
            {listing.footer ? <AnswerBody text={listing.footer} onCite={onCite} /> : null}
          </>
        ) : (
          <AnswerBody
            text={answer.answer}
            onCite={onCite}
            figures={answer.figures ?? []}
            onFigure={(figure) => onCitationClick(figure.node_id)}
          />
        )}
      </div>
      {answer.artifact ? (
        <ArtifactCard
          artifact={answer.artifact}
          onOpen={() => dispatch({ type: "open-artifact", id: answer.artifact?.id ?? null })}
        />
      ) : null}
      {visual ? (
        <div className="msg__visual">
          <McpAppFrame
            result={frameResult}
            onRedrawn={onDrawn}
            onAsk={onAsk}
            onSaved={(id) => {
              queryClient.invalidateQueries({ queryKey: ["artifacts"] });
              dispatch({ type: "open-artifact", id });
              showArtifacts();
            }}
            title={visual.diagram?.title ?? "Visual"}
          />
        </div>
      ) : null}
      {answer.citations.length > 0 ? (
        <SourceStrip
          citations={answer.citations}
          activeChunkId={state.browser.open ? (state.browser.file?.chunkId ?? null) : null}
          onSelect={selectCitation}
          onVisualize={(citation) => draw.mutate([citation])}
          onOpen={(citation) =>
            openFile({
              source: citation.source_id as string,
              path: citation.relative_path as string,
              chunkId: citation.chunk_id ?? null,
            })
          }
        />
      ) : null}
      {browsed ? (
        <p className="msg__hint">
          <button
            type="button"
            className="btn btn--small"
            onClick={() => dispatch({ type: "open-browser", source: browsed.source, path: browsed.path })}
            title="Show this folder in the source browser beside the chat"
          >
            Open in the taxonomy view
          </button>
        </p>
      ) : null}
      {answer.scope ? <AnswerScope scope={answer.scope} /> : null}
      {answer.inventory_hint ? <p className="msg__hint">{answer.inventory_hint}</p> : null}
      {answer.keywords?.unknown ? (
        <p className="msg__hint">
          {answer.keywords.unknown} is not a keyword, so it was searched as written. Type @ at the
          start of a message to see the keywords.
        </p>
      ) : null}
      {answer.steps && answer.steps.length > 1 ? <AgentTrace steps={answer.steps} /> : null}
      {!answer.artifact && answer.mode === "llm" && answerId ? (
        <p className="msg__hint">
          <button data-permission="artifacts.manage"
            type="button"
            className="btn btn--small"
            onClick={() => saveReport.mutate()}
            disabled={saveReport.isPending || saveReport.isSuccess}
            title={
              composed
                ? "Keep the files this answer wrote in the Artifacts tab, with their sources and trace"
                : "Keep this answer as a report in the Artifacts tab, with its diagram and sources"
            }
          >
            {saveReport.isSuccess ? "Saved" : saveReport.isPending ? "Saving…" : composed ? "Save files" : "Save as report"}
          </button>
          {saveReport.isError ? <span className="error small"> {(saveReport.error as Error).message}</span> : null}
        </p>
      ) : null}
      <div className="msg__meta">
        {answer.workflow ? <span className="pill">{answer.workflow}</span> : null}
        {(answer.keywords?.used ?? []).map((keyword) => (
          <span className="pill pill--accent" key={keyword} title="Asked with this keyword">
            {keyword}
          </span>
        ))}
        {answer.route && answer.route.depth !== "short" ? (
          <span className="pill" title={answer.route.why?.depth}>
            {answer.route.depth}
          </span>
        ) : null}
        {answer.mode === "inventory" ? (
          <span title="Answered from the index directly, without searching">
            from the index{answer.inventory ? ` · ${answer.inventory.tool}` : ""}
          </span>
        ) : answer.mode === "search" ? (
          <span title="The ranked hits, with no model writing an answer">search hits · no model</span>
        ) : answer.mode === "llm" ? (
          <span>
            {answer.provider}
            {answer.model ? ` · ${answer.model}` : ""}
          </span>
        ) : (
          <span>extracted passages</span>
        )}
        {answer.mode === "inventory" || answer.mode === "search" ? null : (
          <>
            <span>·</span>
            <span>{answer.citations.length} sources</span>
          </>
        )}
        {answer.facts.length > 0 ? (
          <>
            <span>·</span>
            <span>{answer.facts.length} graph facts</span>
          </>
        ) : null}
        {answer.citations.length > 0 ? (
          <button
            type="button"
            className="btn btn--small"
            disabled={draw.isPending}
            onClick={() => draw.mutate(cited.length ? cited : answer.citations)}
            title="A diagram whose every element cites one of these passages"
          >
            {draw.isPending ? "Drawing…" : "Draw a diagram"}
          </button>
        ) : null}
        {draw.isError ? <span className="error">· {(draw.error as Error).message}</span> : null}
        {answer.error ? <span className="error">· {answer.error}</span> : null}
      </div>
    </div>
  );
}
