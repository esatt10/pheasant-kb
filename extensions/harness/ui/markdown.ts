/**
 * A small Markdown renderer for harness artifacts. Escape first, then add only
 * the handful of tags below, so a document's own text can never become markup.
 * Evidence citations ([E1a2b3c4d]) become buttons the page wires to the
 * evidence list.
 */

const escape = (text: string) =>
  text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

function inline(text: string, figures: Record<string, string>): string {
  let out = escape(text.replace(/\{w=\d+\}/g, "").trim());
  out = out.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, (_m, alt: string, src: string) =>
    figures[src] ? `<img class="hx-md__figure" alt="${alt}" src="${figures[src]}">` : `<em>[${alt}]</em>`);
  out = out.replace(/`([^`]+)`/g, "<code>$1</code>");
  out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/\[(E[0-9a-f]{8})\]/g, '<button type="button" class="hx-cite" data-ev="$1">$1</button>');
  out = out.replace(/\((inference|proposal|unknown|generated illustration)\)/gi,
    '<span class="hx-label">$1</span>');
  return out;
}

const cells = (line: string) => line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());

export function renderMarkdown(source: string, figures: Record<string, string> = {}): string {
  const lines = source.split("\n");
  const html: string[] = [];
  let list = false;
  const closeList = () => { if (list) { html.push("</ul>"); list = false; } };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const heading = /^(#{1,4})\s+(.*)$/.exec(line);
    if (heading) {
      closeList();
      const level = Math.min(heading[1].length + 1, 5);
      html.push(`<h${level}>${inline(heading[2], figures)}</h${level}>`);
      continue;
    }
    if (line.trim().startsWith("|") && /^\s*\|?\s*:?-{3,}/.test(lines[i + 1] ?? "")) {
      closeList();
      const head = cells(line).map((c) => `<th>${inline(c, figures)}</th>`).join("");
      const rows: string[] = [];
      i += 2;
      while (i < lines.length && lines[i].trim().startsWith("|")) {
        rows.push(`<tr>${cells(lines[i]).map((c) => `<td>${inline(c, figures)}</td>`).join("")}</tr>`);
        i++;
      }
      i--;
      html.push(`<table class="hx-md__table"><thead><tr>${head}</tr></thead><tbody>${rows.join("")}</tbody></table>`);
      continue;
    }
    const item = /^\s*[-*]\s+(.*)$/.exec(line);
    if (item) {
      if (!list) { html.push("<ul>"); list = true; }
      html.push(`<li>${inline(item[1], figures)}</li>`);
      continue;
    }
    closeList();
    if (line.trim()) html.push(`<p>${inline(line, figures)}</p>`);
  }
  closeList();
  return html.join("\n");
}
