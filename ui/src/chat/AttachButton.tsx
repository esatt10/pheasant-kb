import { useRef } from "react";

/**
 * The composer's "+": choose files (or a folder) to add. What happens to them
 * is the caller's: the chat sends them to the `uploads` source in the
 * background (the drop zone's path), the harness attaches them to a run. The
 * input accepts every type, because the server decides what it can read --
 * the same rule as the Sources page's drop zone.
 */
export function AttachButton({
  onFiles,
  label = "Add files",
  disabled = false,
  permission,
  busy = false,
}: {
  onFiles: (files: File[]) => void;
  label?: string;
  disabled?: boolean;
  /** The permission the server will ask for; the UI greys the button without it. */
  permission?: string;
  busy?: boolean;
}) {
  const files = useRef<HTMLInputElement>(null);
  const take = (list: FileList | null) => {
    const chosen = Array.from(list ?? []);
    if (chosen.length) onFiles(chosen);
    if (files.current) files.current.value = "";
  };
  return (
    <>
      <input ref={files} type="file" multiple hidden aria-hidden="true" tabIndex={-1}
        onChange={(event) => take(event.target.files)} data-testid="composer-attach-input" />
      <button
        type="button"
        className={`composer__attach${busy ? " composer__attach--busy" : ""}`}
        onClick={() => files.current?.click()}
        disabled={disabled}
        aria-label={label}
        title={`${label} (documents, images, audio, notebooks, archives…)`}
        data-permission={permission}
      >
        +
      </button>
    </>
  );
}
