/** Preserve the source and real directory for every artifact category. */
export function artifactFolder(item: { source?: string | null; folder?: string | null }) {
  const source = item.source || "artifacts";
  const raw = item.folder || "";
  const parts = raw.split("/");
  const dated = /^\d{4}\/\d{2}\/\d{2}\//.test(raw);
  const path = dated ? parts.slice(0, 4).join("/") : raw;
  const scopes: Record<string, string> = {
    org: "Shared knowledge", user: "Personal knowledge", session: "Session knowledge",
  };
  return {
    key: JSON.stringify([source, path]), source, path,
    label: dated ? parts.slice(0, 3).join("-") : scopes[raw] || raw || "Published outputs",
    owner: dated ? parts[3] : "",
    browsable: Boolean(item.source || raw),
  };
}
