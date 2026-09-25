import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { CopyLine } from "./CopyLine";
import { UploadDrop } from "./UploadDrop";

const EXAMPLES = [
  { label: "A folder", value: "/workspace/notes" },
  { label: "A git repo", value: "https://github.com/owner/repo" },
  { label: "A docs site", value: "https://docs.example.com/guide" },
  { label: "Every subfolder", value: "/workspace/clients/*" },
];

/**
 * One field, anything in it.
 *
 * This is the UI half of `pheasant up <target>`: the server does the same
 * detection (folder vs vault vs repo vs URL vs bucket vs connector), clones
 * what needs cloning, registers the source and syncs it. The user picks
 * nothing — no type dropdown, no include globs, no wizard steps.
 *
 * Registration waits for the response; the first sync does not
 * (`wait: false`) — a repo the size of, say, mlflow can take minutes to
 * clone and index, comfortably longer than a browser or reverse proxy is
 * willing to hold a request open (that showed up as a 504 even though the
 * sync went on to succeed server-side). So this form closes the moment the
 * source is registered; indexing continues in the background and its
 * progress shows up on the Sources page/rail (`SourceRecord.syncing`),
 * which now polls while anything is syncing.
 */
export function QuickAdd({ onClose, onAdded }: { onClose: () => void; onAdded: () => void }) {
  const [target, setTarget] = useState("");
  const [split, setSplit] = useState(false);

  const add = useMutation({
    mutationFn: () => api.quickAdd({ target: target.trim(), split, sync_now: true, wait: false }),
    onSuccess: onAdded,
  });

  // Check a filesystem-looking target against the container's mount table
  // *before* the user submits it. The failure this prevents is the single most
  // common first-run one: a real host path that simply is not mounted, which
  // otherwise surfaces as "path does not exist" — true, and useless.
  const looksLikePath = /^([a-zA-Z]:[\\/]|[\\/~])/.test(target.trim());
  const hostPath = useQuery({
    queryKey: ["host-path", target.trim()],
    queryFn: () => api.hostPath(target.trim()),
    enabled: looksLikePath && target.trim().length > 2,
    retry: false,
    staleTime: 30_000,
  });
  const remedy = hostPath.data?.status === "not_mounted" ? hostPath.data.remedy : null;

  return (
    <div className="modal-scrim" onClick={onClose}>
      <div className="modal" onClick={(event) => event.stopPropagation()}>
        <header className="modal__header">
          <h2>Add a source</h2>
          <button className="btn btn--ghost btn--icon" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </header>

        <div className="form-grid">
          <label className="field">
            <span>Paste a path, URL, glob or connector</span>
            <input
              className="text-input"
              autoFocus
              spellCheck={false}
              placeholder="/workspace/notes  ·  https://github.com/owner/repo  ·  notion:workspace"
              value={target}
              onChange={(event) => setTarget(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && target.trim()) add.mutate();
              }}
            />
          </label>

          <div className="sources-strip">
            {EXAMPLES.map((example) => (
              <button
                key={example.value}
                className="source-chip"
                onClick={() => setTarget(example.value)}
              >
                <span className="source-chip__label">
                  {example.label}: {example.value}
                </span>
              </button>
            ))}
          </div>

          <label className="checkbox">
            <input
              type="checkbox"
              checked={split}
              onChange={(event) => setSplit(event.target.checked)}
            />
            Index each subfolder as its own source
          </label>

          <p className="muted small" style={{ margin: 0 }}>
            pheasant detects what it is — repository, Obsidian vault, notes folder,
            document folder, web collection, bucket or connector — then indexes it. The
            equivalent command is <code>pheasant up {target.trim() || "<target>"}</code>.
            Registration is immediate; indexing continues in the background and its
            progress shows up on the Sources page (and the "Syncing…" indicator at the
            top of every page until it finishes).
          </p>

          {remedy ? (
            <div className="banner banner--warn" style={{ marginBottom: 0 }}>
              <strong>That path is not visible inside this container.</strong>
              <p className="small" style={{ margin: "6px 0" }}>
                It exists on your machine, but pheasant runs in a container and only
                sees what is mounted into it. Add the mount, restart, and this path
                becomes <code>{remedy.container_path}</code>:
              </p>
              <CopyLine command={remedy.cli} />
              <p className="muted small" style={{ margin: "6px 0 0" }}>
                That writes the bind mount into <code>docker-compose.override.yml</code>{" "}
                and allow-lists the path. By hand, the compose volume is{" "}
                <code>{remedy.compose_volume}</code>. Or skip mounting entirely and
                upload the files below.
              </p>
            </div>
          ) : null}

          {hostPath.data?.status === "visible" &&
          hostPath.data.container_path !== hostPath.data.host_path ? (
            <div className="banner" style={{ marginBottom: 0 }}>
              That path is mounted here as <code>{hostPath.data.container_path}</code> —
              pheasant will index it from there.
            </div>
          ) : null}

          <details className="upload-fallback">
            <summary>…or upload files or a ZIP instead</summary>
            <p className="muted small" style={{ margin: "8px 0" }}>
              No path, no mount. Files and ZIP archives become a normal source;
              supported files inside ZIP folders are indexed separately.
            </p>
            <UploadDrop
              onUploaded={() => {
                onAdded();
                onClose();
              }}
            />
          </details>

          <div className="banner banner--warn" style={{ marginBottom: 0 }}>
            Search and chat may respond more slowly while a new source is syncing,
            especially for a large one — that settles once it finishes.
          </div>

          {add.isError ? (
            <div className="banner banner--error" style={{ marginBottom: 0 }}>
              {(add.error as Error).message}
            </div>
          ) : null}
        </div>

        <div className="modal__footer">
          <button className="btn" onClick={onClose}>
            Cancel
          </button>
          <button
            className="btn btn--primary"
            onClick={() => add.mutate()}
            disabled={!target.trim() || add.isPending}
          >
            {add.isPending ? "Registering…" : "Add and index"}
          </button>
        </div>
      </div>
    </div>
  );
}
