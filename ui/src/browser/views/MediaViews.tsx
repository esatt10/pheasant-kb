import { useEffect, useState } from "react";
import { api } from "../../api/client";

/**
 * Fetch something the region serves (with the API token) as an object URL,
 * revoked when the view unmounts or asks for something else.
 */
export function useObjectUrl(load: (() => Promise<string>) | null, key: string) {
  const [url, setUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!load) return;
    let cancelled = false;
    let created: string | null = null;
    setUrl(null);
    setError(null);
    load()
      .then((objectUrl) => {
        created = objectUrl;
        if (cancelled) URL.revokeObjectURL(objectUrl);
        else setUrl(objectUrl);
      })
      .catch((reason: unknown) => {
        if (!cancelled) setError(reason instanceof Error ? reason.message : String(reason));
      });
    return () => {
      cancelled = true;
      if (created) URL.revokeObjectURL(created);
    };
    // `key` names what `load` fetches; the closure itself changes every render.
  }, [key]);
  return { url, error };
}

/** An indexed image, from the media store, with fit / actual-size / zoom. */
export function ImageView({ documentId, name, caption }: { documentId: string; name: string; caption?: string | null }) {
  const { url, error } = useObjectUrl(() => api.mediaObjectUrl(documentId), documentId);
  const [zoom, setZoom] = useState<number | "fit">("fit");
  if (error) return <p className="error small viewer-pad">The image could not be loaded: {error}</p>;
  if (!url) return <ViewerLoading label="Loading the image…" />;
  const scale = zoom === "fit" ? null : zoom;
  return (
    <div className="image-view">
      <div className="image-view__bar">
        <button type="button" className={`btn btn--small${zoom === "fit" ? " btn--active" : ""}`} onClick={() => setZoom("fit")}>
          Fit
        </button>
        <button type="button" className={`btn btn--small${zoom === 1 ? " btn--active" : ""}`} onClick={() => setZoom(1)}>
          100%
        </button>
        <button
          type="button"
          className="btn btn--small btn--icon"
          aria-label="Zoom out"
          onClick={() => setZoom((z) => Math.max(0.1, (z === "fit" ? 1 : z) / 1.25))}
        >
          −
        </button>
        <button
          type="button"
          className="btn btn--small btn--icon"
          aria-label="Zoom in"
          onClick={() => setZoom((z) => Math.min(8, (z === "fit" ? 1 : z) * 1.25))}
        >
          +
        </button>
        {scale ? <span className="muted small">{Math.round(scale * 100)}%</span> : null}
      </div>
      <div className={`image-view__stage${zoom === "fit" ? " image-view__stage--fit" : ""}`}>
        <img
          src={url}
          alt={caption || name}
          style={scale ? { width: "auto", maxWidth: "none", transform: `scale(${scale})`, transformOrigin: "top left" } : undefined}
        />
      </div>
      {caption ? (
        <p className="image-view__caption">
          <span className="muted small">Indexed as · </span>
          {caption}
        </p>
      ) : null}
    </div>
  );
}

/** A recording with its transcript beneath; the player only if the bytes are here. */
export function AudioView({
  source,
  path,
  mimeType,
  available,
  transcript,
}: {
  source: string;
  path: string;
  mimeType: string | null;
  available: boolean;
  transcript: string | null;
}) {
  const { url, error } = useObjectUrl(
    available && mimeType ? () => api.documentRawObjectUrl({ source, path, mimeType }) : null,
    `${source}/${path}`,
  );
  return (
    <div className="audio-view">
      {available ? (
        url ? (
          <audio controls src={url} className="audio-view__player" />
        ) : error ? (
          <p className="error small">The recording could not be loaded: {error}</p>
        ) : (
          <ViewerLoading label="Loading the recording…" />
        )
      ) : (
        <p className="muted small">The recording is not readable from this replica; its transcript is.</p>
      )}
      <h4 className="audio-view__heading">Transcript</h4>
      <div className="prose-view">{(transcript ?? "").trim() || <span className="muted">No transcript.</span>}</div>
    </div>
  );
}

/**
 * An HTML page drawn in a frame that can run nothing and fetch nothing: an
 * empty `sandbox` (no scripts, no forms, an opaque origin) and a CSP that
 * admits only inline styles and data images.
 */
export function HtmlView({ html }: { html: string }) {
  const guarded =
    `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:">` +
    `<base target="_blank">` +
    html;
  return <iframe className="html-view" sandbox="" srcDoc={guarded} title="HTML preview" referrerPolicy="no-referrer" />;
}

/** An SVG drawn as an image, which is the one context where its scripts never run. */
export function SvgView({ text }: { text: string }) {
  const url = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(text)}`;
  return (
    <div className="image-view">
      <div className="image-view__stage image-view__stage--fit image-view__stage--checker">
        <img src={url} alt="SVG preview" />
      </div>
    </div>
  );
}

export function ViewerLoading({ label }: { label: string }) {
  return (
    <div className="centered muted small viewer-pad">
      <span className="spinner" /> {label}
    </div>
  );
}
