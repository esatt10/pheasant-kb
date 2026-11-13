import type { FolderHealth } from "../api/types";

/** The same readable share in the taxonomy and the source rail. */
export function HealthPill({ health }: { health?: FolderHealth | null }) {
  if (!health || health.percent === null || (health.percent === 100 && !health.warning)) return null;
  const detail = health.ok !== undefined
    ? `${health.ok} ok, ${health.warning} warning, ${health.error} error beneath`
    : `${health.percent}% of this source reads`;
  return <span className={`pill browser-row__health ${healthClass(health)}`} title={detail}>
    {health.percent}%
  </span>;
}

export function healthClass(health: FolderHealth): string {
  if (health.status === "degraded" || health.error) return "error";
  return health.percent === 100 && !health.warning ? "muted" : "source-status--partial";
}
