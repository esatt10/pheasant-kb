import type { DocumentViewer } from "../api/types";

/** One glyph per viewer, so a folder listing reads at a glance. */
const ICONS: Record<DocumentViewer, string> = {
  markdown: "📝",
  code: "🧩",
  json: "🗂",
  yaml: "⚙",
  table: "▦",
  notebook: "📓",
  html: "🌐",
  svg: "🖌",
  pdf: "📕",
  spreadsheet: "📊",
  slides: "📽",
  document: "📄",
  image: "🖼",
  audio: "🎧",
  text: "📃",
};

export function fileIcon(viewer: DocumentViewer | string | undefined): string {
  return ICONS[(viewer ?? "text") as DocumentViewer] ?? "📃";
}

export function formatBytes(value: number | null | undefined): string {
  let size = Number(value ?? 0);
  for (const unit of ["B", "KB", "MB", "GB"]) {
    if (size < 1024 || unit === "GB") return unit === "B" ? `${size} B` : `${size.toFixed(1)} ${unit}`;
    size /= 1024;
  }
  return `${size.toFixed(1)} GB`;
}
