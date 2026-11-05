/**
 * Dictation: the microphone streamed to a live transcription session, its
 * text written into the composer at the caret as it arrives.
 *
 * The region mints a short-lived secret for one OpenAI Realtime
 * transcription session (`POST /assistant/voice/session`, `gpt-live-transcribe`
 * by default) and the browser does the rest: it records the microphone, turns
 * it into 16-bit PCM at the session's rate, appends it to the session over a
 * WebSocket, and hands every transcript delta to `onText`. No audio passes
 * through the region, and the region's own key never reaches the browser.
 *
 * Stopping is immediate for the person — the microphone is released and the
 * button is off at once, so a new dictation can start without waiting — while
 * the closing session commits what it heard and keeps delivering its last
 * words for a moment before it closes.
 */

import { api } from "../api/client";
import type { VoiceSession } from "../api/types";

/** Where `text` lands, and where the caret goes afterwards. */
export interface Splice {
  value: string;
  caret: number;
}

/**
 * Put `text` in place of `value[start:end]` and return the caret after it.
 *
 * `separate` marks the first words of a new utterance: they are spaced from a
 * word already touching the caret, because a transcript's first delta carries
 * no leading space and "dictated" text glued to the previous word is a typo
 * the person then has to fix by hand. Later deltas carry their own spacing.
 */
export function spliceAtCaret(value: string, start: number, end: number, text: string, separate: boolean): Splice {
  const from = Math.max(0, Math.min(start, value.length));
  const to = Math.max(from, Math.min(end, value.length));
  const before = value.slice(0, from);
  let insert = text;
  if (separate) {
    insert = insert.replace(/^\s+/, "");
    if (before && !/\s$/.test(before) && insert) insert = ` ${insert}`;
  }
  return { value: before + insert + value.slice(to), caret: from + insert.length };
}

/**
 * Linear-interpolation resampler, stateful across buffers so a stream of
 * 128-frame worklet blocks resamples as one signal rather than as many.
 */
export class Resampler {
  private position = 0;
  private last = 0;
  private readonly ratio: number;

  constructor(inputRate: number, outputRate: number) {
    this.ratio = inputRate / outputRate;
  }

  push(input: Float32Array): Float32Array {
    if (input.length === 0) return new Float32Array(0);
    if (this.ratio === 1) return input.slice();
    const out: number[] = [];
    // `position` is where the next output sample sits, in this buffer's
    // index space; -1 < position < 0 is between the previous buffer's last
    // sample and this one's first.
    while (this.position < input.length - 1) {
      const index = Math.floor(this.position);
      const fraction = this.position - index;
      const a = index < 0 ? this.last : input[index];
      const b = input[index + 1];
      out.push(a + (b - a) * fraction);
      this.position += this.ratio;
    }
    this.position -= input.length;
    this.last = input[input.length - 1];
    return Float32Array.from(out);
  }
}

/** Float samples in [-1, 1] as base64 16-bit little-endian PCM. */
export function pcm16Base64(samples: Float32Array): string {
  const bytes = new Uint8Array(samples.length * 2);
  const view = new DataView(bytes.buffer);
  for (let i = 0; i < samples.length; i += 1) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  let binary = "";
  const step = 0x8000;
  for (let i = 0; i < bytes.length; i += step) {
    binary += String.fromCharCode(...bytes.subarray(i, i + step));
  }
  return btoa(binary);
}

/** Why dictation cannot run in this browser, or null when it can. */
export function dictationUnsupported(): string | null {
  if (typeof window === "undefined") return "no browser";
  if (!window.isSecureContext) return "Dictation needs HTTPS (or localhost) for microphone access";
  if (!navigator.mediaDevices?.getUserMedia) return "This browser does not offer microphone access";
  if (typeof WebSocket === "undefined" || typeof AudioContext === "undefined") {
    return "This browser cannot stream audio";
  }
  return null;
}

export interface DictationHandlers {
  /** Transcript text, in order. `separate` marks the first words of an utterance. */
  onText: (text: string, separate: boolean) => void;
  /** The session is open and the microphone is streaming. */
  onLive: (session: VoiceSession) => void;
  /** It ended without being asked to (a refusal, a dropped socket). */
  onError: (message: string) => void;
}

const TAP_WORKLET = `
class PheasantPcmTap extends AudioWorkletProcessor {
  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (channel) this.port.postMessage(channel.slice(0));
    return true;
  }
}
registerProcessor("pheasant-pcm-tap", PheasantPcmTap);
`;

/** Audio is sent in ~100 ms appends: small enough to stream, few enough to be cheap. */
const SEND_INTERVAL_MS = 100;
/** How long a stopped session may keep delivering its last words. */
const DRAIN_MS = 4000;
/** The Realtime API refuses to commit less than 100 ms of audio. */
const MIN_COMMIT_MS = 100;

function isDelta(type: string): boolean {
  return type.endsWith("input_audio_transcription.delta") || type === "transcription.delta";
}

function isCompleted(type: string): boolean {
  return type.endsWith("input_audio_transcription.completed") || type === "transcription.completed";
}

/**
 * One dictation, from `start()` to `stop()`. Single use: the button makes a
 * new one per press, which is what lets the next start while this one drains.
 */
export class Dictation {
  private stream: MediaStream | null = null;
  private context: AudioContext | null = null;
  private socket: WebSocket | null = null;
  private resampler: Resampler | null = null;
  private pending: Float32Array[] = [];
  private sendTimer: number | null = null;
  private sentSamples = 0;
  private sampleRate = 24000;
  private stopped = false;
  private cancelled = false;
  private draining: (() => void) | null = null;
  /** The item the stop's commit created; its transcript is the last one. */
  private finalItem: string | null = null;
  /** Items that have had a delta, so their `completed` is not written twice. */
  private readonly seen = new Set<string>();
  private readonly handlers: DictationHandlers;
  private readonly sessionId: string | null;

  constructor(handlers: DictationHandlers, sessionId: string | null = null) {
    this.handlers = handlers;
    this.sessionId = sessionId;
  }

  async start(): Promise<void> {
    const unsupported = dictationUnsupported();
    if (unsupported) throw new Error(unsupported);
    // Made now, inside the click: a context created after the awaits below
    // may have outlived the gesture and start suspended under autoplay rules.
    this.context = new AudioContext();
    // The microphone first: a refused permission costs no session.
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
    } catch (error) {
      this.release();
      const name = (error as { name?: string })?.name;
      throw new Error(
        name === "NotAllowedError" || name === "SecurityError"
          ? "Microphone access was blocked — allow it in the browser to dictate"
          : name === "NotFoundError"
            ? "No microphone was found"
            : `The microphone could not start (${(error as Error)?.message ?? error})`,
      );
    }
    this.stream = stream;
    try {
      if (this.stopped) return this.release();
      const session = await api.voiceSession(this.sessionId);
      if (this.stopped) return this.release();
      this.sampleRate = session.sample_rate || 24000;
      await this.open(session);
      if (this.stopped) {
        this.release();
        this.socket?.close();
        return;
      }
      await this.capture(stream);
      this.handlers.onLive(session);
    } catch (error) {
      // Whatever failed, the microphone does not stay on.
      this.release();
      if (this.socket && this.socket.readyState <= WebSocket.OPEN) this.socket.close();
      throw error;
    }
  }

  private open(session: VoiceSession): Promise<void> {
    return new Promise((resolve, reject) => {
      const socket = new WebSocket(session.websocket_url, [
        "realtime",
        `openai-insecure-api-key.${session.client_secret}`,
      ]);
      this.socket = socket;
      let opened = false;
      socket.onopen = () => {
        opened = true;
        resolve();
      };
      socket.onmessage = (event) => this.receive(event.data);
      socket.onerror = () => {
        if (!opened) reject(new Error("The transcription service could not be reached"));
      };
      socket.onclose = (event) => {
        if (!opened) {
          reject(new Error(`The transcription service closed the connection (${event.code})`));
          return;
        }
        this.draining?.();
        if (!this.stopped) {
          this.handlers.onError(
            `Dictation ended: the transcription service closed the connection${event.reason ? ` (${event.reason})` : ""}`,
          );
          this.halt();
        }
      };
    });
  }

  private async capture(stream: MediaStream): Promise<void> {
    const context = this.context;
    if (!context) throw new Error("The audio context was closed");
    this.resampler = new Resampler(context.sampleRate, this.sampleRate);
    const source = context.createMediaStreamSource(stream);
    // Pulled by the graph only when it reaches the destination; silent there.
    const sink = context.createGain();
    sink.gain.value = 0;
    sink.connect(context.destination);
    const take = (samples: Float32Array) => {
      if (!this.stopped && this.resampler) this.pending.push(this.resampler.push(samples));
    };
    if (context.audioWorklet) {
      const url = URL.createObjectURL(new Blob([TAP_WORKLET], { type: "application/javascript" }));
      try {
        await context.audioWorklet.addModule(url);
      } finally {
        URL.revokeObjectURL(url);
      }
      const tap = new AudioWorkletNode(context, "pheasant-pcm-tap");
      tap.port.onmessage = (event: MessageEvent<Float32Array>) => take(event.data);
      source.connect(tap);
      tap.connect(sink);
    } else {
      // Older engines: deprecated, but it is the same samples.
      const processor = context.createScriptProcessor(2048, 1, 1);
      processor.onaudioprocess = (event) => take(event.inputBuffer.getChannelData(0).slice(0));
      source.connect(processor);
      processor.connect(sink);
    }
    if (context.state === "suspended") await context.resume();
    this.sendTimer = window.setInterval(() => this.flush(), SEND_INTERVAL_MS);
  }

  private flush(): void {
    if (!this.pending.length || this.socket?.readyState !== WebSocket.OPEN) return;
    const total = this.pending.reduce((sum, chunk) => sum + chunk.length, 0);
    const joined = new Float32Array(total);
    let offset = 0;
    for (const chunk of this.pending) {
      joined.set(chunk, offset);
      offset += chunk.length;
    }
    this.pending = [];
    if (!total) return;
    this.sentSamples += total;
    this.socket.send(JSON.stringify({ type: "input_audio_buffer.append", audio: pcm16Base64(joined) }));
  }

  private receive(data: unknown): void {
    if (this.cancelled) return;
    let event: {
      type?: string;
      item_id?: string;
      delta?: string;
      transcript?: string;
      error?: { message?: string };
    };
    try {
      event = JSON.parse(String(data));
    } catch {
      return;
    }
    const type = String(event.type ?? "");
    const item = String(event.item_id ?? "");
    if (isDelta(type) && event.delta) {
      const first = !this.seen.has(item);
      this.seen.add(item);
      this.handlers.onText(event.delta, first);
    } else if (isCompleted(type)) {
      // A model that sends only the finished transcript still writes it.
      if (!this.seen.has(item) && event.transcript) this.handlers.onText(event.transcript, true);
      this.seen.add(item);
      if (this.draining && this.finalItem !== null && this.finalItem === item) this.draining();
    } else if (type === "input_audio_buffer.committed" && this.draining && this.finalItem === null) {
      this.finalItem = item || null;
    } else if (type === "error") {
      // After a stop, a refused commit of a near-empty buffer is not news.
      if (this.stopped) return;
      this.handlers.onError(event.error?.message || "The transcription service reported an error");
      this.halt();
    }
  }

  /** Release the microphone and audio graph; the socket is the caller's. */
  private release(): void {
    if (this.sendTimer !== null) window.clearInterval(this.sendTimer);
    this.sendTimer = null;
    this.stream?.getTracks().forEach((track) => track.stop());
    this.stream = null;
    void this.context?.close().catch(() => undefined);
    this.context = null;
  }

  /**
   * Abandon: release everything and deliver nothing more. For a question
   * that was just sent — its last words must not land in the next one.
   */
  cancel(): void {
    this.cancelled = true;
    this.draining = null;
    this.halt();
  }

  /** End at once, with nothing more to send (an error, or the socket closed). */
  private halt(): void {
    this.stopped = true;
    this.release();
    if (this.socket && this.socket.readyState <= WebSocket.OPEN) this.socket.close();
  }

  /**
   * Stop: the microphone is released now; what was heard is committed and
   * its last words are delivered as they arrive, for up to `DRAIN_MS`.
   */
  stop(): void {
    if (this.stopped) return;
    this.stopped = true;
    this.release();
    const socket = this.socket;
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      socket?.close();
      return;
    }
    this.flush();
    if ((this.sentSamples / this.sampleRate) * 1000 < MIN_COMMIT_MS) {
      socket.close();
      return;
    }
    socket.send(JSON.stringify({ type: "input_audio_buffer.commit" }));
    const timer = window.setTimeout(() => this.draining?.(), DRAIN_MS);
    this.draining = () => {
      this.draining = null;
      window.clearTimeout(timer);
      if (socket.readyState <= WebSocket.OPEN) socket.close();
    };
  }
}
