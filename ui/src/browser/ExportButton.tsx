import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { ExportJobResult, JobRecord } from "../api/types";
import { exportJobFor, takeExport, wantExport } from "../state/background";

type Format = "zip" | "jsonl" | "parquet";

const FORMATS: { value: Format; label: string; hint: string }[] = [
  { value: "zip", label: "ZIP", hint: "Every file at its path, originals kept, with a manifest" },
  { value: "jsonl", label: "JSONL", hint: "One line per document: metadata, text, outline" },
  { value: "parquet", label: "Parquet", hint: "The same rows as one table, for DuckDB or pandas" },
];

/**
 * "Export this source": every document of one source as one file to keep.
 *
 * A background job (`POST /sources/{name}/exports`, MCP `start_source_export`):
 * **Download** starts it and returns at once, the panel shows its progress
 * while it is open, and the file downloads when it is ready -- whether the
 * panel is still open or not, because the page (`state/background`) remembers
 * what this tab asked for and the jobs tray, always mounted, fetches it. So the
 * person can close the panel and keep working; the tray says how far it is and
 * offers the file again afterwards. Asking again is idempotent: the server
 * returns the job already running, or the finished file when nothing changed.
 *
 * Reading, not managing: it needs `knowledge.use`, as reading a document or
 * downloading an artifact does, so it carries no `data-permission` gate. The
 * server applies the listing's scope and ACL, so it never holds a document
 * the caller could not open.
 *
 * Two placements, one panel. The taxonomy view's header opens it under an
 * **Export source** pill beside **Search this source** (`ExportButton`), and
 * inside a folder as **Export folder**, which sends that folder as
 * `path_prefix`; the Sources page opens it as a row under the source, the way
 * **outline** opens (`ExportPanel`), because a floating panel is clipped by the
 * table there and runs off a phone's screen.
 */
export function ExportPanel({
  source,
  path = "",
  onBusy,
}: {
  source: string;
  /** A folder (or file) inside the source; empty for the whole source. */
  path?: string;
  onBusy?: (busy: boolean) => void;
}) {
  const [format, setFormat] = useState<Format>("zip");
  const [chunks, setChunks] = useState(false);
  const key = `${source}/${path}/${format}/${chunks}`;
  const [jobId, setJobId] = useState<string | undefined>(() => exportJobFor(key));
  useEffect(() => setJobId(exportJobFor(key)), [key]);

  const start = useMutation({
    mutationFn: () => api.startSourceExport(source, { format, includeChunks: chunks, path }),
    onSuccess: (job) => {
      setJobId(job.job_id);
      if (job.reused === "ready") {
        // Finished earlier and nothing changed: the same file, saved now.
        wantExport(key, job.job_id);
        takeExport(job.job_id);
        const ready = job.result as unknown as ExportJobResult | null;
        for (const file of ready?.files ?? []) {
          void api.downloadJobFile(job.job_id, file.part, file.filename);
        }
        return;
      }
      // Running (new, or the same request already under way): the tray saves
      // it when it finishes, wherever the person is by then.
      wantExport(key, job.job_id);
    },
  });

  const job = useQuery({
    queryKey: ["job", jobId],
    queryFn: () => api.job(jobId!),
    enabled: Boolean(jobId),
    refetchInterval: (query) => (query.state.data?.active === false ? false : 1000),
  });
  const record: JobRecord | undefined = job.data;
  const running = Boolean(record?.active) || start.isPending;
  useEffect(() => onBusy?.(running), [running, onBusy]);

  const result = record?.status === "succeeded" ? (record.result as unknown as ExportJobResult) : null;
  const status = start.isPending
    ? "Starting…"
    : record?.active
      ? record.progress.total
        ? `Exporting ${record.progress.current.toLocaleString()} of ${record.progress.total.toLocaleString()} documents · you can close this and keep working`
        : "Preparing… you can close this and keep working"
      : result
        ? `Ready · ${result.documents.toLocaleString()} documents in ${result.files.length === 1 ? result.files[0].filename : `${result.files.length} files`}`
        : record?.status === "failed"
          ? null
          : null;

  return (
    <div className="export-control__body">
      <p className="export-control__title">
        Export <strong>{path ? `${source} / ${path}` : source}</strong>
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
          disabled={running}
          onClick={() => start.mutate()}
        >
          {running ? "Exporting…" : `Download ${FORMATS.find((f) => f.value === format)!.label}`}
        </button>
        {result
          ? result.files.map((file) => (
              <button
                key={file.part}
                type="button"
                className="btn btn--small"
                onClick={() => {
                  takeExport(jobId!);
                  void api.downloadJobFile(jobId!, file.part, file.filename);
                }}
              >
                Save {file.filename} again
              </button>
            ))
          : null}
        {status ? (
          <span className="muted small" role="status">
            {status}
          </span>
        ) : null}
      </div>
      {start.isError || record?.status === "failed" ? (
        <p className="error small" role="alert">
          {start.isError ? (start.error as Error).message : record?.error}
        </p>
      ) : null}
    </div>
  );
}

/** The taxonomy header's pill: the panel, floating under it. */
export function ExportButton({ source, path = "" }: { source: string; path?: string }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const box = useRef<HTMLSpanElement>(null);
  const what = path ? "folder" : "source";

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
        title={`Download every document of ${path ? `${source} / ${path}` : source} as one file`}
      >
        <span className="scope-toggle__icon" aria-hidden>
          ⤓
        </span>
        {busy && open ? "Exporting…" : `Export ${what}`}
      </button>
      {open ? (
        <div
          className="export-control__panel"
          role="dialog"
          aria-label={`Export ${path ? `${source} / ${path}` : source}`}
        >
          <ExportPanel key={`${source}/${path}`} source={source} path={path} onBusy={setBusy} />
        </div>
      ) : null}
    </span>
  );
}
