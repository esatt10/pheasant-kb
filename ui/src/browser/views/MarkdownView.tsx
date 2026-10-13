import DOMPurify from "dompurify";
import yaml from "js-yaml";
import { Marked } from "marked";
import type { Tokens } from "marked";
import { useEffect, useMemo, useRef } from "react";
import type { MouseEvent as ReactMouseEvent } from "react";
import { api } from "../../api/client";
import { escapeHtml, highlightLines, languageFor } from "../highlight";

export interface MarkdownSection {
  /** Anchor id, unique within the document. */
  id: string;
  level: number;
  title: string;
  /** 1-based line the section starts on (its heading). */
  line: number;
  body: string;
}

/**
 * The document cut at its headings (outside fenced code), so each section is
 * rendered on its own and can be scrolled to, outlined, and marked when a
 * cited passage falls inside it.
 */
export function splitSections(text: string): { front: string | null; sections: MarkdownSection[] } {
  let lines = text.split("\n");
  let offset = 0;
  let front: string | null = null;
  if (lines[0]?.trim() === "---") {
    const close = lines.slice(1, 200).findIndex((line) => line.trim() === "---" || line.trim() === "...");
    if (close >= 0) {
      front = lines.slice(1, close + 1).join("\n");
      offset = close + 2;
      lines = lines.slice(offset);
    }
  }
  const sections: MarkdownSection[] = [];
  const used = new Map<string, number>();
  let fence: string | null = null;
  let current: MarkdownSection = { id: "top", level: 0, title: "", line: offset + 1, body: "" };
  const body: string[] = [];
  const flush = () => {
    current.body = body.join("\n");
    if (current.level > 0 || current.body.trim()) sections.push(current);
    body.length = 0;
  };
  lines.forEach((line, index) => {
    const fenceMatch = /^\s{0,3}(```+|~~~+)/.exec(line);
    if (fenceMatch) {
      if (fence === null) fence = fenceMatch[1][0];
      else if (fenceMatch[1][0] === fence) fence = null;
    }
    const heading = fence === null ? /^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$/.exec(line) : null;
    if (!heading) {
      body.push(line);
      return;
    }
    flush();
    const title = heading[2].replace(/[*_`]/g, "").trim();
    const base = slug(title) || "section";
    const seen = used.get(base) ?? 0;
    used.set(base, seen + 1);
    current = {
      id: seen ? `${base}-${seen}` : base,
      level: heading[1].length,
      title,
      line: offset + index + 1,
      body: "",
    };
  });
  flush();
  return { front, sections };
}

export function slug(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\s-]/gu, "")
    .trim()
    .replace(/\s+/g, "-");
}

/** Tags a document may not bring: anything that fetches, runs, or submits. */
const FORBID_TAGS = [
  "img",
  "picture",
  "source",
  "video",
  "audio",
  "iframe",
  "object",
  "embed",
  "style",
  "link",
  "meta",
  "form",
  "button",
  "script",
];

function sanitize(html: string): string {
  return DOMPurify.sanitize(html, {
    USE_PROFILES: { html: true },
    FORBID_TAGS,
    // A `style` attribute can fetch through `url(...)`.
    FORBID_ATTR: ["style", "srcset"],
  });
}

function makeParser(): Marked {
  return new Marked({
    gfm: true,
    renderer: {
      image({ href, text, title }: Tokens.Image) {
        // Never an <img> pointing wherever the document says. Indexed images
        // are loaded through `/media` after render; remote ones not at all.
        return `<span class="md-figure" data-src="${escapeHtml(href ?? "")}" data-alt="${escapeHtml(
          text ?? "",
        )}" title="${escapeHtml(title ?? text ?? "")}"></span>`;
      },
      code({ text, lang }: Tokens.Code) {
        const language = languageFor((lang ?? "").split(/\s/)[0], undefined);
        const body = highlightLines(text, language).join("\n");
        return `<pre class="md-code" data-language="${escapeHtml(lang ?? "")}"><code class="hljs">${body}</code></pre>`;
      },
    },
  });
}

/**
 * Markdown rendered as a reader expects: sections, a working outline, links
 * that move around the source in place, and figures from the index.
 */
export function MarkdownView({
  text,
  documentId,
  source,
  path,
  focusLine,
  scrollTo,
  onOpenPath,
  onBrowse,
}: {
  text: string;
  documentId: string;
  source: string;
  path: string;
  focusLine?: number | null;
  scrollTo?: { id: string; nonce: number } | null;
  onOpenPath: (path: string, anchor?: string) => void;
  onBrowse: (folder: string) => void;
}) {
  const parser = useMemo(makeParser, []);
  const { front, sections } = useMemo(() => splitSections(text), [text]);
  const rendered = useMemo(
    () => sections.map((section) => sanitize(parser.parse(section.body, { async: false }) as string)),
    [parser, sections],
  );
  const frontData = useMemo(() => readFront(front), [front]);
  const rootRef = useRef<HTMLDivElement>(null);

  const focusedId = useMemo(() => {
    if (!focusLine) return null;
    let hit: string | null = null;
    for (const section of sections) {
      if (section.line <= focusLine) hit = section.id;
    }
    return hit;
  }, [focusLine, sections]);

  useEffect(() => {
    const id = scrollTo?.id ?? focusedId;
    if (!id) return;
    rootRef.current?.querySelector(`[data-section="${CSS.escape(id)}"]`)?.scrollIntoView({ block: "start" });
  }, [scrollTo?.id, scrollTo?.nonce, focusedId, rendered]);

  // Figures: an image the document links, if the index holds it, is drawn from
  // `/media` (its id follows the stable artifact grammar, branch included).
  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;
    const created: string[] = [];
    let cancelled = false;
    root.querySelectorAll<HTMLElement>(".md-figure").forEach((holder) => {
      const src = holder.dataset.src ?? "";
      const alt = holder.dataset.alt ?? "";
      holder.replaceChildren();
      if (/^[a-z][a-z0-9+.-]*:/i.test(src) || src.startsWith("//")) {
        holder.classList.add("md-figure--remote");
        holder.textContent = `Remote image not loaded${alt ? `: ${alt}` : ""}`;
        return;
      }
      const target = resolvePath(path, src);
      holder.textContent = alt || target;
      holder.dataset.path = target;
      api
        .mediaObjectUrl(siblingId(documentId, source, target))
        .then((url) => {
          if (cancelled) {
            URL.revokeObjectURL(url);
            return;
          }
          created.push(url);
          const image = document.createElement("img");
          image.src = url;
          image.alt = alt;
          holder.replaceChildren(image);
          holder.classList.add("md-figure--loaded");
        })
        .catch(() => holder.classList.add("md-figure--missing"));
    });
    return () => {
      cancelled = true;
      created.forEach((url) => URL.revokeObjectURL(url));
    };
  }, [rendered, documentId, source, path]);

  const onClick = (event: ReactMouseEvent<HTMLDivElement>) => {
    const target = event.target;
    if (!(target instanceof Element)) return;
    const figure = target.closest<HTMLElement>(".md-figure[data-path]");
    if (figure?.dataset.path) {
      onOpenPath(figure.dataset.path);
      return;
    }
    const anchor = target.closest<HTMLAnchorElement>("a[href]");
    if (!anchor) return;
    const href = anchor.getAttribute("href") ?? "";
    event.preventDefault();
    if (href.startsWith("#")) {
      const id = decodeURIComponent(href.slice(1));
      rootRef.current
        ?.querySelector(`[data-section="${CSS.escape(id)}"], [id="${CSS.escape(id)}"]`)
        ?.scrollIntoView({ block: "start" });
      return;
    }
    if (/^[a-z][a-z0-9+.-]*:/i.test(href) || href.startsWith("//")) {
      if (/^(https?|mailto):/i.test(href)) window.open(href, "_blank", "noopener,noreferrer");
      return;
    }
    const [linked, fragment] = href.split("#");
    const resolved = resolvePath(path, linked.split("?")[0]);
    if (!resolved) return;
    if (/\.[a-z0-9]{1,8}$/i.test(resolved)) {
      onOpenPath(resolved, fragment ? safeDecode(fragment) : undefined);
    }
    else onBrowse(resolved.replace(/\/$/, ""));
  };

  return (
    <div className="md-view" ref={rootRef} onClick={onClick}>
      {frontData ? <FrontMatter data={frontData} /> : null}
      {sections.map((section, index) => {
        const Heading = `h${Math.min(6, Math.max(1, section.level))}` as "h1";
        return (
          <section
            key={section.id}
            data-section={section.id}
            className={`md-section${section.id === focusedId ? " md-section--focus" : ""}`}
          >
            {section.level > 0 ? (
              <Heading id={section.id} className="md-heading">
                {section.title}
                <a className="md-heading__anchor" href={`#${section.id}`} aria-label="Link to this section">
                  #
                </a>
              </Heading>
            ) : null}
            <div dangerouslySetInnerHTML={{ __html: rendered[index] }} />
          </section>
        );
      })}
    </div>
  );
}

function FrontMatter({ data }: { data: Record<string, unknown> }) {
  const entries = Object.entries(data);
  return (
    <details className="md-front" open>
      <summary>Properties · {entries.length}</summary>
      <table>
        <tbody>
          {entries.map(([key, value]) => (
            <tr key={key}>
              <th>{key}</th>
              <td>{formatValue(value)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </details>
  );
}

function readFront(front: string | null): Record<string, unknown> | null {
  if (!front) return null;
  try {
    const parsed = yaml.load(front);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

function formatValue(value: unknown): string {
  if (Array.isArray(value)) return value.map(formatValue).join(", ");
  if (value instanceof Date) return value.toISOString().slice(0, 10);
  if (value && typeof value === "object") return JSON.stringify(value);
  return String(value ?? "");
}

/** `href` relative to the directory holding `from`, normalized; "" if it leaves the source. */
export function resolvePath(from: string, href: string): string {
  const decoded = safeDecode(href.trim());
  const base = decoded.startsWith("/") ? [] : from.split("/").slice(0, -1);
  for (const part of decoded.split("/")) {
    if (!part || part === ".") continue;
    if (part === "..") {
      if (!base.length) return "";
      base.pop();
    } else base.push(part);
  }
  return base.join("/");
}

function safeDecode(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

/**
 * The artifact id of another file in the same source and branch:
 * `file:{source}:{relpath}:branch={b}` (docs/graph_model.md), with the branch
 * read off the document that links to it.
 */
export function siblingId(documentId: string, source: string, path: string): string {
  const branch = /:branch=([^:]*)$/.exec(documentId)?.[1] ?? "none";
  return `file:${source}:${path}:branch=${branch}`;
}
