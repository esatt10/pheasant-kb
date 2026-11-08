import type { SourceRecord } from "../api/types";

/** A source's status with the share of it the index can read.
 *
 * "healthy · 99.9%" says what "degraded" alone could not: whether a few pages
 * failed or most of them did. The tooltip names what failed; the score already
 * leaves out anything removed or archived since the sync.
 */
export function SourceStatus({ source }: { source: SourceRecord }) {
  const status = source.last_status ?? "—";
  const health = source.health;
  if (!health || health.percent === null || health.unreadable === 0) {
    return <span>{status}</span>;
  }
  const failed = health.unreadable_sample
    .map((item) => (item.error ? `${item.path}: ${item.error}` : item.path))
    .join("\n");
  const title =
    `${health.readable} of ${health.readable + health.unreadable} readable` +
    (health.dropped ? `; ${health.dropped} dropped since the sync` : "") +
    (failed ? `\n${failed}` : "");
  return (
    <span className={health.status === "degraded" ? "error" : "source-status--partial"} title={title}>
      {status} · {health.percent}%
    </span>
  );
}
