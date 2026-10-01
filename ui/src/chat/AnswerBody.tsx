import DOMPurify from "dompurify";
import { Marked } from "marked";
import type { Tokens } from "marked";
import { useMemo } from "react";
import type { ReactNode } from "react";
import type { Figure } from "../api/types";
import { FigureImage } from "./FigureImage";

/**
 * Render the assistant's Markdown while keeping citation markers connected to
 * the source strip. Markdown is model-authored, so its HTML is sanitized before
 * it reaches the DOM; raw HTML and remote images are not part of the answer
 * format.
 */
export function AnswerBody({
  text,
  onCite,
  figures = [],
  onFigure,
}: {
  text: string;
  onCite: (index: number) => void;
  figures?: Figure[];
  onFigure?: (figure: Figure) => void;
}) {
  const renderer = useMemo(() => new Marked(markdownOptions), []);
  return <>{renderBlocks(text, onCite, figures, onFigure, renderer)}</>;
}

const markdownOptions = {
  gfm: true,
  extensions: [
    {
      name: "pheasantCitation",
      level: "inline" as const,
      start(source: string) {
        const index = source.search(/\[(?:\d{1,2}|fig:\d{1,2})\]/);
        return index < 0 ? undefined : index;
      },
      tokenizer(source: string) {
        const match = /^\[(?:(\d{1,2})|fig:(\d{1,2}))\]/.exec(source);
        if (!match) return undefined;
        return {
          type: "pheasantCitation",
          raw: match[0],
          index: match[1] ? Number(match[1]) : undefined,
          figure: match[2] ? Number(match[2]) : undefined,
        };
      },
      renderer(token: Tokens.Generic) {
        if (token.figure !== undefined) {
          return `<span class="cite-chip cite-chip--figure" title="A figure shown in this answer">fig ${token.figure}</span>`;
        }
        const index = token.index ?? 0;
        return `<button type="button" class="cite-chip" data-cite="${index}" title="Show source ${index}">${index}</button>`;
      },
    },
  ],
  renderer: {
    html({ text }: { text: string }) {
      return escapeHtml(text);
    },
    image() {
      // Indexed figures use the authenticated media path below. Never fetch
      // an arbitrary image URL authored by a model.
      return "";
    },
  },
};

function renderBlocks(
  text: string,
  onCite: (index: number) => void,
  figures: Figure[],
  onFigure: ((figure: Figure) => void) | undefined,
  parser: Marked,
) {
  const out: ReactNode[] = [];
  // Keep fenced code verbatim, and only promote a standalone verified figure
  // marker to an image. Inline markers stay in the Markdown renderer.
  text.split(/```/).forEach((segment, segmentIndex) => {
    if (segmentIndex % 2 === 1) {
      const newline = segment.indexOf("\n");
      const language = newline > 0 ? segment.slice(0, newline).trim() : "";
      const body = newline > 0 ? segment.slice(newline + 1) : segment;
      out.push(
        <pre className="answer-code" key={`code-${segmentIndex}`} data-language={language || undefined}>
          <code>{body.replace(/\n$/, "")}</code>
        </pre>,
      );
      return;
    }

    let prose: string[] = [];
    const flush = (suffix: string) => {
      const source = prose.join("\n");
      if (source.trim()) {
        const html = DOMPurify.sanitize(parser.parse(source, { async: false }), {
          USE_PROFILES: { html: true },
        });
        out.push(
          <div
            className="answer-markdown"
            key={`${segmentIndex}-${suffix}`}
            onClick={(event) => {
              const target = event.target;
              if (!(target instanceof Element)) return;
              const button = target.closest<HTMLButtonElement>("button[data-cite]");
              if (!button || !event.currentTarget.contains(button)) return;
              const index = Number(button.dataset.cite);
              if (Number.isInteger(index) && index > 0) onCite(index);
            }}
            dangerouslySetInnerHTML={{ __html: html }}
          />,
        );
      }
      prose = [];
    };

    segment.split("\n").forEach((line, lineIndex) => {
      const match = line.match(/^\s*\[fig:(\d{1,2})\]\s*$/);
      const figure = match
        ? figures.find((candidate) => candidate.figure === Number(match[1]))
        : undefined;
      if (!figure) {
        prose.push(line);
        return;
      }
      flush(`before-${lineIndex}`);
      out.push(
        <FigureImage
          key={`${segmentIndex}-figure-${lineIndex}`}
          figure={figure}
          onOpen={() => onFigure?.(figure)}
        />,
      );
    });
    flush("end");
  });
  return out;
}

function escapeHtml(value: string): string {
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
