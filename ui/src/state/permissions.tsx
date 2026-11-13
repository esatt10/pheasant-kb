import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, onApiAuthChanged } from "../api/client";

const Capabilities = createContext<string[]>([]);
const MANAGED = ["sources.manage", "artifacts.manage", "tuning.manage", "evaluation.manage",
  "settings.manage", "security.manage", "jobs.manage"];

export function PermissionProvider({ children }: { children: ReactNode }) {
  const [generation, setGeneration] = useState(0);
  const client = useQueryClient();
  useEffect(() => onApiAuthChanged(() => {
    setGeneration(value => value + 1);
    void client.invalidateQueries({ queryKey: ["capabilities"] });
  }), [client]);
  const identity = useQuery({ queryKey: ["capabilities", generation], queryFn: api.me,
    retry: false, refetchInterval: 30_000, staleTime: 10_000 });
  const permissions = identity.data?.permissions ?? [];
  const hidden = MANAGED.filter(permission => !permissions.includes(permission))
    .map(permission => `[data-permission="${permission}"] { display: none !important; }`).join("\n");
  return <Capabilities.Provider value={permissions}>
    <style>{hidden}</style>
    {children}
  </Capabilities.Provider>;
}

export function usePermissions() {
  const permissions = useContext(Capabilities);
  return { can: (permission: string) => permissions.includes(permission) };
}

export function PermissionPage({ permission, children }: { permission: string; children: ReactNode }) {
  const { can } = usePermissions();
  return can(permission) ? <>{children}</> : <div className="page">
    <p>This page requires {permission}. Search, chat, browsing, and downloads remain available.</p>
  </div>;
}
