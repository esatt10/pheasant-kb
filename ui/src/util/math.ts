import katex from "katex";
import "katex/dist/katex.min.css";
import type { MarkedExtension, Tokens } from "marked";

/**
 * TeX mathematics in Markdown: what an answer writes and what an indexed
 * document carries, rendered with KaTeX.
 *
 * Math is tokenized *inside* `marked`, not found in its output afterwards,
 * because Markdown would otherwise get there first: `$a_1 * b_2$` is an
 * emphasis run to Markdown, and `\[` is an escaped bracket. The token renders
 * to an empty placeholder carrying the TeX as an attribute, so the sanitizer
 * sees nothing but a `<span data-tex>`; `renderMath` fills placeholders
 * *after* sanitizing. KaTeX's output needs `style` attributes, which the
 * document views forbid for anything a file brings — the order keeps that
 * rule for the file and lets the typesetter, which is code we ship, through.
 *
 * KaTeX runs with `trust: false`, so `\href`, `\url`, `\includegraphics` and
 * the `\html*` commands are refused, and every colour is validated by its
 * parser: a formula cannot fetch, link or style beyond the typeset glyphs.
 *
 * Delimiters: `$…$` and `\(…\)` inline; `$$…$$` and `\[…\]` display, on a
 * line of their own or inside a sentence; and a bare display environment
 * (`\begin{align}…\end{align}`) on its own lines. A `$` only opens math when
 * a non-space follows it and only closes when a non-space precedes it and no
 * digit follows, so "costs $5 and $10" stays prose.
 */

/** Display environments KaTeX typesets on their own, without `$$`. */
const ENVIRONMENTS =
  "equation|align|aligned|gather|gathered|alignat|multline|split|eqnarray|CD|cases|matrix|pmatrix|bmatrix|vmatrix|Vmatrix";

const BLOCK_PATTERNS: RegExp[] = [
  /^ {0,3}\$\$([\s\S]+?)\$\$[ \t]*(?:\n|$)/,
  /^ {0,3}\\\[([\s\S]+?)\\\][ \t]*(?:\n|$)/,
  new RegExp(String.raw`^ {0,3}(\\begin\{(${ENVIRONMENTS})\*?\}[\s\S]+?\\end\{\2\*?\})[ \t]*(?:\n|$)`),
];

const INLINE_DISPLAY: RegExp[] = [/^\$\$((?:\\[\s\S]|[^\\$])+?)\$\$/, /^\\\[([\s\S]+?)\\\]/];
const INLINE_PAREN = /^\\\(([\s\S]+?)\\\)/;
const INLINE_DOLLAR = /^\$((?:\\[\s\S]|[^\\$])+?)\$(?!\d)/;

function blockMatch(source: string): RegExpExecArray | null {
  for (const pattern of BLOCK_PATTERNS) {
    const match = pattern.exec(source);
    if (match) return match;
  }
  return null;
}

function inlineMatch(source: string): { raw: string; tex: string; display: boolean } | null {
  for (const pattern of INLINE_DISPLAY) {
    const match = pattern.exec(source);
    if (match) return { raw: match[0], tex: match[1], display: true };
  }
  const paren = INLINE_PAREN.exec(source);
  if (paren) return { raw: paren[0], tex: paren[1], display: false };
  const dollar = INLINE_DOLLAR.exec(source);
  // Currency, not math: "$5 and $10" opens on a digit and closes after a space.
  if (dollar && !/^\s|\s$/.test(dollar[1]) && !/\n\s*\n/.test(dollar[1])) {
    return { raw: dollar[0], tex: dollar[1], display: false };
  }
  return null;
}

/** The `marked` extensions; pass to `new Marked(...)` or `marked.use(...)`. */
export const mathExtension: MarkedExtension = {
  extensions: [
    {
      name: "mathBlock",
      level: "block",
      start(source: string) {
        // Cut a paragraph only where a block really starts, or a `$$x$$`
        // opening a line of prose would split its paragraph in two.
        const candidates = /(?:^|\n)( {0,3})(?:\$\$|\\\[|\\begin\{)/g;
        for (let found = candidates.exec(source); found; found = candidates.exec(source)) {
          const at = found.index + (found[0].startsWith("\n") ? 1 : 0);
          if (blockMatch(source.slice(at))) return at;
        }
        return undefined;
      },
      tokenizer(source: string) {
        const match = blockMatch(source);
        if (!match) return undefined;
        return { type: "mathBlock", raw: match[0], tex: match[1].trim() };
      },
      renderer(token: Tokens.Generic) {
        return `${placeholder(String(token.tex), true, "div")}\n`;
      },
    },
    {
      name: "mathInline",
      level: "inline",
      start(source: string) {
        const index = source.search(/\$|\\\(|\\\[/);
        return index < 0 ? undefined : index;
      },
      tokenizer(source: string) {
        const match = inlineMatch(source);
        if (!match) return undefined;
        return { type: "mathInline", raw: match.raw, tex: match.tex.trim(), display: match.display };
      },
      renderer(token: Tokens.Generic) {
        // A span even when displayed: a <div> inside the paragraph would be
        // split out of it by the HTML parser.
        return placeholder(String(token.tex), Boolean(token.display), "span");
      },
    },
  ],
};

function placeholder(tex: string, display: boolean, tag: "div" | "span"): string {
  return `<${tag} class="math math--${display ? "display" : "inline"}" data-tex="${escapeAttribute(
    tex,
  )}"${display ? ' data-display="true"' : ""}></${tag}>`;
}

const OPTIONS = {
  throwOnError: false,
  trust: false,
  strict: "ignore" as const,
  output: "htmlAndMathml" as const,
  maxSize: 20,
  maxExpand: 1000,
};

/** Typeset formulas, keyed by mode and source; a streaming answer re-renders often. */
const cache = new Map<string, string>();
const CACHE_LIMIT = 500;

/** One formula as HTML. Never throws: a bad formula is shown as its source. */
export function typeset(tex: string, display: boolean): string {
  const source = repairJsonEscapes(tex);
  const key = `${display ? "D" : "I"}${source}`;
  const hit = cache.get(key);
  if (hit !== undefined) return hit;
  let html: string;
  try {
    html = katex.renderToString(source, { ...OPTIONS, displayMode: display });
  } catch {
    // Past `maxExpand`, or anything else KaTeX refuses outright.
    html = `<code class="math-error">${escapeAttribute(display ? `$$${tex}$$` : `$${tex}$`)}</code>`;
  }
  if (cache.size >= CACHE_LIMIT) cache.delete(cache.keys().next().value as string);
  cache.set(key, html);
  return html;
}

/**
 * Fill the math placeholders in already sanitized HTML. Returns the input
 * untouched when it holds none, which is every answer without math.
 */
export function renderMath(html: string): string {
  if (!html.includes("data-tex=")) return html;
  const template = document.createElement("template");
  template.innerHTML = html;
  template.content.querySelectorAll<HTMLElement>("[data-tex]").forEach((holder) => {
    holder.innerHTML = typeset(holder.dataset.tex ?? "", holder.dataset.display === "true");
  });
  return template.innerHTML;
}

/**
 * Undo the escapes a JSON reply made of TeX written with single backslashes.
 *
 * An answer that crossed a JSON parse (the combined grade-and-answer path)
 * and was written `"\frac"` comes back as a form feed and `rac`; `\beta` as a
 * backspace, `\theta` as a tab. Neither control character means anything in
 * TeX, so restoring the command is unambiguous. A newline is left alone,
 * because inside display math it is ordinary whitespace.
 */
function repairJsonEscapes(tex: string): string {
  return tex.replace(/\f/g, "\\f").replace(/\x08/g, "\\b").replace(/\t(?=[a-zA-Z])/g, "\\t");
}

function escapeAttribute(value: string): string {
  return value.replace(/[&<>"']/g, (character) => `&#${character.charCodeAt(0)};`);
}
