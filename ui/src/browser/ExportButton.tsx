import { useEffect, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api } from "../api/client";

type Format = "zip" | "jsonl" | "parquet";

const FORMATS: { value: Format; label: string; hint: string }[] = [
  { value: "zip", label: "ZIP", hint: "Every file at its path, originals kept, with a manifest" },
  { value: "jsonl", label: "JSONL", hint: "One line per document: metadata, text, outline" },
  { value: "parquet", label: "Parquet", hint: "The same rows as one table, for DuckDB or pandas" },
];

/**
 * "Export this source": every document of one source as one file to keep —
 * `GET /sources/{name}/export?download=true`, the operation MCP
 * `export_source` calls. A source past the server's part size downloads as
 * several numbered files, and the panel says how far it is.
 *
 * Reading, not managing: it needs `knowledge.use`, as reading a document or
 * downloading an artifact does, so it carries no `data-permission` gate. The
 * server applies the listing's scope and ACL, so it never holds a document
 * the caller could not open.
 *
 * Two placements, one panel. The taxonomy view's header opens it under an
 * **Export source** pill beside **Search this source** (`ExportButton`); the
 * Sources page opens it as a row under the source, the way **outline** opens
 * (`ExportPanel`), because a floating panel is clipped by the table there and
 * runs off a phone's screen.
 */
export function ExportPanel({ source }: { source: string }) {
  const [format, setFormat] = useState<Format>("zip");
  const [chunks, setChunks] = useState(false);
  const [progress, setProgress] = useState<string | null>(null);

  const run = useMutation({
    mutationFn: () =>
      api.downloadSourceExport(source, { format, includeChunks: chunks }, (part) =>
        setProgress(`${part.done.toLocaleString()} of ${part.total.toLocaleString()} documents`),
      ),
    onMutate: () => setProgress("Preparing…"),
    onSuccess: (result) =>
      setProgress(
        `Saved ${result.files.length === 1 ? result.files[0] : `${result.files.length} files`} · ${result.documents.toLocaleString()} documents`,
      ),
    onError: () => setProgress(null),
  });

  return (
    <div className="export-control__body">
      <p className="export-control__title">
        Export <strong>{source}</strong>
      </p>
      <div className="export-control__formats" role="radiogroup" aria-label="Format">
        {FORMATS.map((option) => (
          <label key={option.value} className={`export-control__format${format === option.value ? " is-selected" : ""}`}>
            <input
              type="radio"
              name={`export-format-${source}`}
              value={option.value}
              checked={format === option.value}
              onChange={() => setFormat(option.value)}
            />
            <span className="export-control__label">{option.label}</span>
            <span className="muted small">{option.hint}</span>
          </label>
        ))}
      </div>
      <label className="export-control__check small">
        <input type="checkbox" checked={chunks} onChange={(event) => setChunks(event.target.checked)} />
        Include chunks (line spans and heading paths)
      </label>
      <div className="export-control__actions">
        <button
          type="button"
          className="btn btn--small btn--primary"
          disabled={run.isPending}
          onClick={() => run.mutate()}
        >
          {run.isPending ? "Exporting…" : `Download ${FORMATS.find((f) => f.value === format)!.label}`}
        </button>
        {progress ? (
          <span className="muted small" role="status">
            {progress}
          </span>
        ) : null}
      </div>
      {run.isError ? (
        <p className="error small" role="alert">
          {(run.error as Error).message}
        </p>
      ) : null}
    </div>
  );
}

/** The taxonomy header's pill: the panel, floating under it. */
export function ExportButton({ source }: { source: string }) {
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    if (!open) return;
    const onPointer = (event: PointerEvent) => {
      if (box.current && !box.current.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => event.key === "Escape" && setOpen(false);
    document.addEventListener("pointerdown", onPointer);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onPointer);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <span className="export-control" ref={box}>
      <button
        type="button"
        className={`scope-toggle${open ? " scope-toggle--active" : ""}`}
        aria-expanded={open}
        aria-haspopup="dialog"
        onClick={() => setOpen((value) => !value)}
        title={`Download every document of ${source} as one file`}
      >
        <span className="scope-toggle__icon" aria-hidden>
          ⤓
        </span>
        Export source
      </button>
      {open ? (
        <div className="export-control__panel" role="dialog" aria-label={`Export ${source}`}>
          <ExportPanel source={source} />
        </div>
      ) : null}
    </span>
  );
}
