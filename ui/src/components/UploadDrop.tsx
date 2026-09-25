import { useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api } from "../api/client";

/**
 * Drop supported files or ZIP archives here and they become a real source.
 *
 * The zero-setup path: no path to type, no directory to mount, no source type
 * to pick. Files are posted to `/sources/upload`, land in a directory under
 * `/state/uploads`, and that directory is registered as an ordinary
 * `document_folder` source — so they flow through the same connector → chunk →
 * graph pipeline as everything else and can be removed by deleting the source.
 *
 * Indexing is handed to the background (`wait: false`) for the same reason
 * quick-add does it: a large drop can take longer than a browser or reverse
 * proxy will hold a request open. Progress shows up in the jobs tray.
 */
export function UploadDrop({
  onUploaded,
  sourceName = "uploads",
}: {
  onUploaded?: () => void;
  sourceName?: string;
}) {
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const upload = useMutation({
    mutationFn: (files: File[]) => api.uploadDocuments(files, sourceName),
    onSuccess: () => onUploaded?.(),
  });

  const send = (list: FileList | null) => {
    const files = Array.from(list ?? []);
    if (files.length > 0) upload.mutate(files);
  };

  return (
    <div className="upload">
      <div
        className={`upload__zone${dragging ? " upload__zone--active" : ""}`}
        onDragOver={(event) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          event.preventDefault();
          setDragging(false);
          send(event.dataTransfer.files);
        }}
        onClick={() => inputRef.current?.click()}
        role="button"
        tabIndex={0}
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === " ") inputRef.current?.click();
        }}
      >
        <input
          ref={inputRef}
          type="file"
          multiple
          hidden
          onChange={(event) => {
            send(event.target.files);
            // Clear it, or picking the same file twice in a row is a no-op.
            event.target.value = "";
          }}
        />
        <strong>{upload.isPending ? "Uploading…" : "Drop files or ZIP archives here"}</strong>
        <span className="muted small">
          Markdown, PDF, Office, images, audio, code, configs, or a ZIP containing
          any mix of supported files in folders — or click to choose. ZIP members
          are indexed separately in <code>{sourceName}</code>.
        </span>
      </div>

      {upload.isError ? (
        <div className="banner banner--error" style={{ marginBottom: 0 }}>
          {(upload.error as Error).message}
        </div>
      ) : null}

      {upload.isSuccess ? (
        <div className="banner" style={{ marginBottom: 0 }}>
          Stored {upload.data.stored.length} file
          {upload.data.stored.length === 1 ? "" : "s"} in <code>{upload.data.source_name}</code>
          {upload.data.syncing ? " — indexing now, progress is in the jobs tray." : "."}
          {upload.data.rejected.length > 0 ? (
            <ul className="small" style={{ margin: "0.5rem 0 0" }}>
              {upload.data.rejected.map((item) => (
                <li key={item.filename}>
                  {item.filename}: {item.error}
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
