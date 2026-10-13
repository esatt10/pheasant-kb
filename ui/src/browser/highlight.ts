import hljs from "highlight.js/lib/core";
import bash from "highlight.js/lib/languages/bash";
import c from "highlight.js/lib/languages/c";
import clojure from "highlight.js/lib/languages/clojure";
import cmake from "highlight.js/lib/languages/cmake";
import cpp from "highlight.js/lib/languages/cpp";
import csharp from "highlight.js/lib/languages/csharp";
import css from "highlight.js/lib/languages/css";
import dart from "highlight.js/lib/languages/dart";
import diff from "highlight.js/lib/languages/diff";
import dockerfile from "highlight.js/lib/languages/dockerfile";
import dos from "highlight.js/lib/languages/dos";
import elixir from "highlight.js/lib/languages/elixir";
import elm from "highlight.js/lib/languages/elm";
import erlang from "highlight.js/lib/languages/erlang";
import fsharp from "highlight.js/lib/languages/fsharp";
import go from "highlight.js/lib/languages/go";
import graphql from "highlight.js/lib/languages/graphql";
import groovy from "highlight.js/lib/languages/groovy";
import haskell from "highlight.js/lib/languages/haskell";
import ini from "highlight.js/lib/languages/ini";
import java from "highlight.js/lib/languages/java";
import javascript from "highlight.js/lib/languages/javascript";
import json from "highlight.js/lib/languages/json";
import julia from "highlight.js/lib/languages/julia";
import kotlin from "highlight.js/lib/languages/kotlin";
import latex from "highlight.js/lib/languages/latex";
import less from "highlight.js/lib/languages/less";
import lua from "highlight.js/lib/languages/lua";
import makefile from "highlight.js/lib/languages/makefile";
import markdown from "highlight.js/lib/languages/markdown";
import nim from "highlight.js/lib/languages/nim";
import objectivec from "highlight.js/lib/languages/objectivec";
import ocaml from "highlight.js/lib/languages/ocaml";
import perl from "highlight.js/lib/languages/perl";
import php from "highlight.js/lib/languages/php";
import plaintext from "highlight.js/lib/languages/plaintext";
import powershell from "highlight.js/lib/languages/powershell";
import properties from "highlight.js/lib/languages/properties";
import protobuf from "highlight.js/lib/languages/protobuf";
import python from "highlight.js/lib/languages/python";
import r from "highlight.js/lib/languages/r";
import ruby from "highlight.js/lib/languages/ruby";
import rust from "highlight.js/lib/languages/rust";
import scala from "highlight.js/lib/languages/scala";
import scss from "highlight.js/lib/languages/scss";
import sql from "highlight.js/lib/languages/sql";
import swift from "highlight.js/lib/languages/swift";
import typescript from "highlight.js/lib/languages/typescript";
import vbnet from "highlight.js/lib/languages/vbnet";
import xml from "highlight.js/lib/languages/xml";
import yaml from "highlight.js/lib/languages/yaml";

/**
 * Syntax highlighting for the file viewer.
 *
 * Core plus the languages pheasant indexes, registered by hand rather than
 * `highlight.js/lib/common`, so the bundle carries exactly what a source can
 * hold. The output is highlight.js's own: escaped text inside `<span
 * class="hljs-…">`, which is the only markup it emits — and it is sanitized
 * again before it reaches the DOM anyway, because file content is untrusted.
 */
const LANGUAGES = {
  bash,
  c,
  clojure,
  cmake,
  cpp,
  csharp,
  css,
  dart,
  diff,
  dockerfile,
  dos,
  elixir,
  elm,
  erlang,
  fsharp,
  go,
  graphql,
  groovy,
  haskell,
  ini,
  java,
  javascript,
  json,
  julia,
  kotlin,
  latex,
  less,
  lua,
  makefile,
  markdown,
  nim,
  objectivec,
  ocaml,
  perl,
  php,
  plaintext,
  powershell,
  properties,
  protobuf,
  python,
  r,
  ruby,
  rust,
  scala,
  scss,
  sql,
  swift,
  typescript,
  vbnet,
  xml,
  yaml,
};

for (const [name, language] of Object.entries(LANGUAGES)) {
  hljs.registerLanguage(name, language);
}

/** The region's language names that highlight.js spells differently. */
const ALIASES: Record<string, string> = {
  shell: "bash",
  objc: "objectivec",
  toml: "ini",
  hcl: "ini",
  html: "xml",
  svg: "xml",
  zig: "rust",
  solidity: "javascript",
  prisma: "graphql",
};

/** By extension, for a file the region did not name a language for. */
const BY_EXTENSION: Record<string, string> = {
  ".md": "markdown",
  ".mdx": "markdown",
  ".markdown": "markdown",
  ".json": "json",
  ".jsonc": "json",
  ".json5": "json",
  ".yaml": "yaml",
  ".yml": "yaml",
  ".html": "xml",
  ".htm": "xml",
  ".xml": "xml",
  ".svg": "xml",
  ".csv": "plaintext",
  ".tsv": "plaintext",
  ".ipynb": "json",
  ".tex": "latex",
};

/** Past this, highlighting costs more than it gives; the text is shown plain. */
export const HIGHLIGHT_LIMIT = 400_000;

export function languageFor(language: string | null | undefined, extension?: string): string {
  const wanted = (language ?? "").toLowerCase();
  const named = ALIASES[wanted] ?? wanted;
  if (named && hljs.getLanguage(named)) return named;
  const byExtension = extension ? BY_EXTENSION[extension.toLowerCase()] : undefined;
  return byExtension ?? "plaintext";
}

/** Highlighted HTML, one entry per source line, each line's spans balanced. */
export function highlightLines(text: string, language: string): string[] {
  if (text.length > HIGHLIGHT_LIMIT || language === "plaintext") {
    return text.split("\n").map(escapeHtml);
  }
  let html: string;
  try {
    html = hljs.highlight(text, { language, ignoreIllegals: true }).value;
  } catch {
    return text.split("\n").map(escapeHtml);
  }
  return splitBalanced(html);
}

/**
 * Split highlighted HTML at newlines without breaking a span that crosses one
 * (a block comment, a multi-line string): each line closes what is open at its
 * end and the next line reopens it, so every line is valid markup on its own.
 */
export function splitBalanced(html: string): string[] {
  const lines: string[] = [];
  const open: string[] = [];
  let current = "";
  const tag = /<span[^>]*>|<\/span>|\n/g;
  let last = 0;
  for (let match = tag.exec(html); match; match = tag.exec(html)) {
    current += html.slice(last, match.index);
    last = match.index + match[0].length;
    if (match[0] === "\n") {
      lines.push(current + "</span>".repeat(open.length));
      current = open.join("");
    } else if (match[0] === "</span>") {
      open.pop();
      current += match[0];
    } else {
      open.push(match[0]);
      current += match[0];
    }
  }
  current += html.slice(last);
  lines.push(current + "</span>".repeat(open.length));
  return lines;
}

export function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (character) => {
    switch (character) {
      case "&":
        return "&amp;";
      case "<":
        return "&lt;";
      case ">":
        return "&gt;";
      case '"':
        return "&quot;";
      default:
        return "&#39;";
    }
  });
}
