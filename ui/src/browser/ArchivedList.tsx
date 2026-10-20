import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { ArchivedDocument } from "../api/types";
import { useArchiveRefresh } from "./archive";

/**
 * The documents removed from this folder (and below), with **Reinstate**.
 *
 * Read from `GET /documents/archived`, the list `list_archived_documents`
 * returns. Reinstating syncs the source, which reads the file again, so the
 * document reappears in the listing once that sync lands. An upload is not
 * reinstatable: removing it deleted it, and it is listed only until the
 * indexer has done so.
 */
export function ArchivedList({ source, path }: { source: string; path: string }) {
  const [open, setOpen] = useState(false);
  const refresh = useArchiveRefresh();
  const archived = useQuery({
    queryKey: ["archived", source, path],
    queryFn: () => api.archivedDocuments({ source, pathPrefix: path }),
  });
  const reinstate = useMutation({
    mutationFn: (document: ArchivedDocument) => api.reinstateDocuments([`${document.source_name}/${document.path}`]),
    onSuccess: () => refresh({ again: true }),
  });

  const documents = archived.data?.documents ?? [];
  if (!documents.length) return null;
  return (
    <div className="archived-list">
      <button
        type="button"
        className="link-button archived-list__toggle"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        {open ? "▾" : "▸"} Removed here · {archived.data?.total ?? documents.length}
      </button>
      {open ? (
        <ul className="archived-list__rows">
          {documents.map((document) => (
            <li key={`${document.source_name}/${document.path}`} className="archived-list__row">
              <span className="archived-list__name" title={`${document.source_name}/${document.path}`}>
                {document.path.slice(path ? path.length + 1 : 0)}
              </span>
              {document.reinstatable ? (
                <button
                  type="button"
                  className="btn btn--small"
                  disabled={reinstate.isPending}
                  onClick={() => reinstate.mutate(document)}
                  title="Index it again: its source syncs and reads the file"
                >
                  Reinstate
                </button>
              ) : (
                <span className="pill pill--warn" title="An uploaded file: removing it deleted it">
                  deleting
                </span>
              )}
            </li>
          ))}
        </ul>
      ) : null}
      {reinstate.isError ? <p className="error small">{(reinstate.error as Error).message}</p> : null}
    </div>
  );
}
