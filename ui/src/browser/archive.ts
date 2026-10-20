import { useCallback } from "react";
import { useQueryClient } from "@tanstack/react-query";

/** Queries whose answer changes when a document leaves or rejoins the index. */
const AFFECTED = new Set([
  "browse",
  "document-content",
  "archived",
  "documents",
  "overview",
  "sources",
  "knowledge-base",
  "taxonomy",
  "graph",
  "graph-path",
  "horizon-graph",
  "node-content",
  "explain",
]);

/**
 * Refetch what a removal or a reinstatement changed: the folder listings, the
 * archived list, the open document. A reinstatement syncs in the background,
 * so it refetches again once that sync has had a moment to land.
 */
export function useArchiveRefresh(): (options?: { again?: boolean }) => void {
  const queryClient = useQueryClient();
  return useCallback(
    (options) => {
      const refresh = () =>
        queryClient.invalidateQueries({ predicate: (query) => AFFECTED.has(String(query.queryKey[0])) });
      refresh();
      if (options?.again) {
        for (const delay of [1500, 5000]) window.setTimeout(refresh, delay);
      }
    },
    [queryClient],
  );
}
