import { createContext, useContext, type ComponentType, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import bundledExtensions from "virtual:pheasant-extensions";
import { request } from "./api/client";
import type { SourceRecord } from "./api/types";
import { registerExtensionGraphColors } from "./graph/graphStyles";
import { usePermissions } from "./state/permissions";

export interface SourceEditorProps {
  source?: SourceRecord;
  onClose: () => void;
}

export interface SourceUi {
  type: string;
  addLabel: string;
  Editor: ComponentType<SourceEditorProps>;
  FolderDetails?: ComponentType<{ source: SourceRecord; path: string }>;
  appearance: (source: SourceRecord) => {
    label: string;
    pathLabel: string;
    editLabel?: string;
    browseLabel?: string;
    outline?: boolean;
    link?: { label: string; to: string };
  };
}

/** A tab an extension adds to the Admin page, offered only with `permission`. */
export interface AdminTabUi {
  id: string;
  label: string;
  permission: string;
  Panel: ComponentType;
}

export interface UiExtension {
  id: string;
  apiVersion: 1;
  routes: readonly { path: string; label: string; Page: ComponentType }[];
  sources?: readonly SourceUi[];
  graphColors?: Readonly<Record<string, string>>;
  adminTabs?: readonly AdminTabUi[];
}

export const bundledUiExtensions = bundledExtensions;
for (const extension of bundledExtensions) {
  if (extension.graphColors) registerExtensionGraphColors(extension.graphColors);
}
const ActiveExtensions = createContext<readonly UiExtension[]>([]);

export function ExtensionsProvider({ children }: { children: ReactNode }) {
  const { can } = usePermissions();
  const connected = can("knowledge.use");
  const installed = useQuery({
    queryKey: ["region-extensions", connected],
    enabled: connected && bundledExtensions.length > 0,
    queryFn: () => request<{ api_version: number; extensions: { id: string; ui_routes: string[] }[] }>("/extensions"),
    retry: false,
  });
  const active = connected && installed.data?.api_version === 1
    ? bundledExtensions.filter((extension) => installed.data?.extensions.some((entry) =>
      entry.id === extension.id && extension.routes.every((route) => entry.ui_routes.includes(route.path))))
    : [];
  return <ActiveExtensions.Provider value={active}>{children}</ActiveExtensions.Provider>;
}

export function useUiExtensions() {
  return useContext(ActiveExtensions);
}
