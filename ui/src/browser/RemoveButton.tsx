import { useEffect, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api } from "../api/client";
import { useArchiveRefresh } from "./archive";

/**
 * "Remove from the index", for the open file: `POST /documents/archive`, the
 * operation `archive_documents` and `@remove` call.
 *
 * Two clicks, because removing is not a view setting: the first arms the
 * button, the second removes. A folder or connector file is archived and
 * listed under **Removed** in the taxonomy view, where it can be reinstated;
 * an uploaded file is deleted.
 */
export function RemoveButton({ source, path, onRemoved }: { source: string; path: string; onRemoved: () => void }) {
  const [armed, setArmed] = useState(false);
  const refresh = useArchiveRefresh();
  const remove = useMutation({
    mutationFn: () => api.archiveDocuments([`${source}/${path}`]),
    onSuccess: () => {
      refresh();
      onRemoved();
    },
  });
  useEffect(() => {
    if (!armed) return;
    const timer = window.setTimeout(() => setArmed(false), 4000);
    return () => window.clearTimeout(timer);
  }, [armed]);

  return (
    <>
      <button
        type="button"
        className={`scope-toggle scope-toggle--remove${armed ? " scope-toggle--armed" : ""}`}
        disabled={remove.isPending}
        onClick={() => (armed ? remove.mutate() : setArmed(true))}
        title={
          armed
            ? "Click again to remove it from the index"
            : `Remove ${source}/${path} from the index and keep it out of later syncs. ` +
              "Files from a folder can be reinstated from the Removed list; uploaded files are deleted."
        }
      >
        <span className="scope-toggle__icon" aria-hidden>
          🗑
        </span>
        {remove.isPending ? "Removing…" : armed ? "Confirm remove" : "Remove"}
      </button>
      {remove.isError ? (
        <span className="error small" role="alert">
          {(remove.error as Error).message}
        </span>
      ) : null}
    </>
  );
}
