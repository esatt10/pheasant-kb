import { useRef, useState } from "react";
import { startUpload, useBackground } from "../state/background";

/**
 * Drop supported files or ZIP archives here and they become a real source.
 *
 * The zero-setup path: no path to type, no directory to mount, no source type
 * to pick. Files are posted to `/sources/upload`, land in a directory under
 * `/state/uploads`, and that directory is registered as an ordinary
 * `document_folder` source — so they flow through the same connector → chunk →
 * graph pipeline as everything else and can be removed by deleting the source.
 *
 * In the background, all of it: the bytes are sent by the page (`startUpload`),
 * not by this component, so closing it or moving to another page does not
 * cancel anything; the server answers as soon as they arrive with an `upload`
 * job that lands, registers and indexes them. The jobs tray shows the upload,
 * then that job. The same files dropped again while they are sending are not
 * sent twice, and a file the source already holds byte for byte is not stored
 * twice.
 */
export function UploadDrop({
  onUploaded,
  sourceName = "uploads",
}: {
  onUploaded?: () => void;
  sourceName?: string;
}) {
  const [dragging, setDragging] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const { transfers } = useBackground();
  const sending = transfers.filter((t) => t.source === sourceName && t.status === "uploading");

  const send = (list: FileList | null) => {
    const files = Array.from(list ?? []);
    if (files.length === 0) return;
    const started = startUpload(files, sourceName);
    setNote(
      started
        ? `Uploading ${files.length} file${files.length === 1 ? "" : "s"} in the background — progress is in the jobs tray. You can keep working.`
        : "Those files are already uploading.",
    );
    if (started) onUploaded?.();
  };

  return (
    <div className="upload" data-permission="sources.manage">
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
        <strong>
          {sending.length > 0
            ? `Uploading ${sending.reduce((n, t) => n + t.names.length, 0)} file(s)… drop more any time`
            : "Drop files or ZIP archives here"}
        </strong>
        <span className="muted small">
          Markdown, PDF, Office, images, audio, code, configs, or a ZIP containing
          any mix of supported files in folders — or click to choose. ZIP members
          are indexed separately in <code>{sourceName}</code>.
        </span>
      </div>

      {note ? (
        <div className="banner" role="status" style={{ marginBottom: 0 }}>
          {note}
        </div>
      ) : null}
    </div>
  );
}
