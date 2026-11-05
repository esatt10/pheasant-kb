import { useCallback, useEffect, useRef, useState } from "react";
import { Dictation } from "./dictation";

export type DictationState = "off" | "starting" | "live";

interface DictationOptions {
  sessionId: string | null;
  /** The cap on one dictation, from `GET /assistant/voice`. */
  maxSeconds: number;
  /** Transcript text to write at the caret; `separate` starts an utterance. */
  onText: (text: string, separate: boolean) => void;
  /** Called once the microphone is live, e.g. to put the caret back in view. */
  onLive?: () => void;
}

export interface DictationControl {
  state: DictationState;
  /** Whole seconds left in this dictation while live. */
  remaining: number;
  /** Why the last dictation stopped on its own, or failed to start. */
  notice: string | null;
  toggle: () => void;
  /** Stop and keep its last words (the person pressed the button). */
  stop: () => void;
  /** Stop and drop anything still arriving (a question was just sent). */
  cancel: () => void;
}

/**
 * One dictation at a time, capped at `maxSeconds`. Each start is a new
 * `Dictation`, so a stopped one finishes delivering its last words while the
 * next is already listening — there is no wait between dictations.
 */
export function useDictation({ sessionId, maxSeconds, onText, onLive }: DictationOptions): DictationControl {
  const [state, setState] = useState<DictationState>("off");
  const [remaining, setRemaining] = useState(maxSeconds);
  const [notice, setNotice] = useState<string | null>(null);
  const current = useRef<Dictation | null>(null);
  const timer = useRef<number | null>(null);
  // The handlers outlive a render; read the newest callbacks through refs.
  const textRef = useRef(onText);
  textRef.current = onText;
  const liveRef = useRef(onLive);
  liveRef.current = onLive;

  const clearTimer = () => {
    if (timer.current !== null) window.clearInterval(timer.current);
    timer.current = null;
  };

  const end = useCallback((how: "stop" | "cancel") => {
    clearTimer();
    const dictation = current.current;
    current.current = null;
    if (dictation) {
      if (how === "stop") dictation.stop();
      else dictation.cancel();
    }
    setState("off");
  }, []);

  const start = useCallback(() => {
    setNotice(null);
    setRemaining(maxSeconds);
    setState("starting");
    const dictation = new Dictation(
      {
        onText: (text, separate) => {
          textRef.current(text, separate);
        },
        onLive: () => {
          if (current.current !== dictation) return;
          setState("live");
          const startedAt = performance.now();
          const limit = maxSeconds * 1000;
          clearTimer();
          timer.current = window.setInterval(() => {
            const elapsed = performance.now() - startedAt;
            setRemaining(Math.max(0, Math.ceil((limit - elapsed) / 1000)));
            if (elapsed >= limit && current.current === dictation) {
              end("stop");
              setNotice(
                `Dictation stopped at the ${maxSeconds >= 60 && maxSeconds % 60 === 0 ? `${maxSeconds / 60}-minute` : `${maxSeconds}-second`} limit`,
              );
            }
          }, 200);
          liveRef.current?.();
        },
        onError: (message) => {
          if (current.current !== dictation) return;
          clearTimer();
          current.current = null;
          setState("off");
          setNotice(message);
        },
      },
      sessionId,
    );
    current.current = dictation;
    dictation.start().catch((error: Error) => {
      if (current.current !== dictation) return;
      current.current = null;
      clearTimer();
      setState("off");
      setNotice(error.message || "Dictation could not start");
    });
  }, [end, maxSeconds, sessionId]);

  const toggle = useCallback(() => {
    if (current.current) end("stop");
    else start();
  }, [end, start]);

  // Leaving the chat stops the microphone; nothing keeps listening unseen.
  useEffect(() => () => end("cancel"), [end]);

  return {
    state,
    remaining,
    notice,
    toggle,
    stop: useCallback(() => end("stop"), [end]),
    cancel: useCallback(() => end("cancel"), [end]),
  };
}

function MicIcon({ on }: { on: boolean }) {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="9" y="2" width="6" height="12" rx="3" fill={on ? "currentColor" : "none"} />
      <path d="M5 10a7 7 0 0 0 14 0" />
      <line x1="12" y1="17" x2="12" y2="22" />
    </svg>
  );
}

function clock(seconds: number): string {
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

interface MicButtonProps {
  control: DictationControl;
  /** Why the microphone cannot be used here; the button is disabled with it. */
  unavailable: string | null;
}

/** The composer's dictation toggle: on (listening, with the time left) or off. */
export function MicButton({ control, unavailable }: MicButtonProps) {
  const { state, remaining } = control;
  const on = state !== "off";
  const label = unavailable
    ? `Dictation unavailable: ${unavailable}`
    : state === "live"
      ? `Stop dictation (${clock(remaining)} left)`
      : state === "starting"
        ? "Starting dictation… (click to cancel)"
        : "Dictate with your microphone";
  return (
    <span className="composer__mic">
      {state === "live" ? (
        <span className="composer__mic-time" aria-hidden="true">
          {clock(remaining)}
        </span>
      ) : null}
      <button
        type="button"
        className={`composer__mic-btn${on ? " composer__mic-btn--on" : ""}${state === "starting" ? " composer__mic-btn--starting" : ""}`}
        // Keep focus (and the caret the transcript is written at) in the box.
        onMouseDown={(event) => event.preventDefault()}
        onClick={control.toggle}
        disabled={Boolean(unavailable)}
        aria-pressed={on}
        aria-label={on ? "Stop dictation" : "Start dictation"}
        title={label}
        data-state={state}
      >
        <MicIcon on={on} />
      </button>
    </span>
  );
}
