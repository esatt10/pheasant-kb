/**
 * The question in flight, kept across a reload so its answer is collected.
 *
 * The conversation itself is never written to storage (`state/session.tsx`),
 * and that stays true: what is kept is one question and the id its answer is
 * held under, from the moment it is asked until its answer or refusal lands.
 * A phone browser that discards a backgrounded tab reloads it on return, and
 * without this the answer the region went on writing had no one to collect it.
 */

const KEY = "pheasant.pending-answer";
/** The region keeps an answer this long (`services/answer_resume.py`). */
const HELD_FOR_MS = 60 * 60 * 1000;

export interface PendingAnswer {
  /** The chat turn's id. */
  id: string;
  question: string;
  /** What the region keeps the answer under. */
  answerId: string;
  askedAt: number;
}

/** A fresh id for an answer: 32 hex characters, unguessable. */
export function newAnswerId(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

export function rememberPending(pending: PendingAnswer): void {
  try {
    sessionStorage.setItem(KEY, JSON.stringify(pending));
  } catch {
    /* storage unavailable: a reload just cannot resume this one */
  }
}

/** Forget ``id``'s question once it has its answer (or a newer one replaced it). */
export function forgetPending(id: string): void {
  try {
    const held = pendingAnswer();
    if (!held || held.id === id) sessionStorage.removeItem(KEY);
  } catch {
    /* nothing to forget */
  }
}

/** The question a previous page load left waiting, if the region still holds its answer. */
export function pendingAnswer(): PendingAnswer | null {
  try {
    const raw = sessionStorage.getItem(KEY);
    if (!raw) return null;
    const held = JSON.parse(raw) as Partial<PendingAnswer>;
    if (
      typeof held.id !== "string" ||
      typeof held.question !== "string" ||
      typeof held.answerId !== "string" ||
      typeof held.askedAt !== "number" ||
      Date.now() - held.askedAt > HELD_FOR_MS
    ) {
      sessionStorage.removeItem(KEY);
      return null;
    }
    return held as PendingAnswer;
  } catch {
    return null;
  }
}
