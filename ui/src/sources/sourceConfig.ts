import type { SourceRecord } from "../api/types";

export function sourceConfig(source?: SourceRecord | null): Record<string, unknown> {
  if (!source) return {};
  if (source.config_json) {
    try {
      return JSON.parse(source.config_json);
    } catch {
      return source;
    }
  }
  return source;
}
