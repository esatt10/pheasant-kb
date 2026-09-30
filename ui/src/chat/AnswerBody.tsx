import { Fragment } from "react";
import type { ReactNode } from "react";
import type { Figure } from "../api/types";
import { FigureImage } from "./FigureImage";

/**
 * Renders an answer as rich text, turning `[1]` markers into clickable chips.
 *
 * The chips are the whole point of the surface: a claim in the prose and the
 * passage it came from are one click apart, and clicking also highlights the
 * cited node on the graph. That is why this is a small hand-rolled renderer
 * rather than a markdown library — the citation splitting has to own the text
 * nodes, and a general renderer would fight it for them (and drag in a parser
 * plus sanitizer for a bundle that is already 770kB).
 *
 * Supported, because it is what models actually emit in grounded answers:
 * headings, **bold**, *italic*, `inline code`, ```fenced code```, bullet and
 * numbered lists, > blockquotes, --- rules, and [links](url). Anything else
 * renders as its literal text — never as raw HTML, since nothing here builds
 * markup from model output.
 *
 * `[fig:n]` alone on a line shows figure n — an image a cited document holds —
 * and inline it is a chip. The image comes from the answer's verified
 * `figures` list, never from the text: the model picks a number, the server
 * already dropped any number that names no figure.
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
  return <>{renderBlocks(text, onCite, figures, onFigure)}</>;
}

function renderBlocks(
  text: string,
  onCite: (index: number) => void,
  figures: Figure[] = [],
  onFigure?: (figure: Figure) => void,
): ReactNode[] {
  const out: ReactNode[] = [];
  // Split fenced code out first: its contents must survive verbatim, with no
  // inline formatting and no citation chips inside.
  const segments = text.split(/```/);
  segments.forEach((segment, segmentIndex) => {
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
    segment.split(/\n{2,}/).forEach((block, blockIndex) => {
      const key = `${segmentIndex}-${blockIndex}`;
      // A figure line can share a block with prose; split it out so the image
      // stands on its own and the prose around it still renders as prose.
      let prose: string[] = [];
      const flush = (suffix: string) => {
        if (prose.length) out.push(...renderBlock(prose.join("\n"), `${key}-${suffix}`, onCite));
        prose = [];
      };
      block.split("\n").forEach((line, lineIndex) => {
        const only = line.match(/^\s*\[fig:(\d{1,2})\]\s*$/);
        const figure = only ? figures.find((f) => f.figure === Number(only[1])) : undefined;
        if (!figure) {
          prose.push(line);
          return;
        }
        flush(`p${lineIndex}`);
        out.push(
          <FigureImage key={`${key}-f${lineIndex}`} figure={figure} onOpen={() => onFigure?.(figure)} />,
        );
      });
      flush("end");
    });
  });
  return out;
}

function renderBlock(
  block: string,
  key: string,
  onCite: (index: number) => void,
): ReactNode[] {
  const lines = block.split("\n").filter((line) => line.trim());
  if (lines.length === 0) return [];

  if (lines.every((line) => /^\s*(---|\*\*\*|___)\s*$/.test(line))) {
    return [<hr key={key} />];
  }

  const heading = lines[0].match(/^(#{1,4})\s+(.*)$/);
  if (heading && lines.length === 1) {
    const level = heading[1].length;
    const Tag = (["h3", "h4", "h5", "h6"] as const)[level - 1];
    return [<Tag key={key}>{inline(heading[2], onCite)}</Tag>];
  }

  if (lines.every((line) => /^\s*>\s?/.test(line))) {
    return [
      <blockquote key={key}>
        {inline(lines.map((line) => line.replace(/^\s*>\s?/, "")).join(" "), onCite)}
      </blockquote>,
    ];
  }

  if (lines.every((line) => /^\s*[-*•]\s+/.test(line))) {
    return [
      <ul key={key}>
        {lines.map((line, i) => (
          <li key={i}>{inline(line.replace(/^\s*[-*•]\s+/, ""), onCite)}</li>
        ))}
      </ul>,
    ];
  }

  if (lines.every((line) => /^\s*\d+[.)]\s+/.test(line))) {
    return [
      <ol key={key}>
        {lines.map((line, i) => (
          <li key={i}>{inline(line.replace(/^\s*\d+[.)]\s+/, ""), onCite)}</li>
        ))}
      </ol>,
    ];
  }

  return [<p key={key}>{inline(block, onCite)}</p>];
}

/**
 * One pass over a run of text, splitting on whichever inline construct comes
 * first. Ordering matters: citations before emphasis, so `[1]` next to bold
 * text still becomes a chip, and code before emphasis so `*` inside code is
 * literal.
 */
const INLINE =
  /(\[fig:\d{1,2}\])|(\[\d{1,2}\])|(`[^`]+`)|(\*\*[^*]+\*\*)|(\*[^*\n]+\*)|(\[[^\]]+\]\([^)\s]+\))/;

function inline(text: string, onCite: (index: number) => void): ReactNode[] {
  const out: ReactNode[] = [];
  let rest = text;
  let key = 0;
  while (rest) {
    const match = rest.match(INLINE);
    if (!match || match.index === undefined) {
      out.push(rest);
      break;
    }
    if (match.index > 0) out.push(<Fragment key={key++}>{rest.slice(0, match.index)}</Fragment>);
    const token = match[0];

    if (token.startsWith("[fig:")) {
      out.push(
        <span key={key++} className="cite-chip cite-chip--figure" title="A figure shown in this answer">
          fig {token.slice(5, -1)}
        </span>,
      );
    } else if (/^\[\d{1,2}\]$/.test(token)) {
      const index = Number(token.slice(1, -1));
      out.push(
        <button
          key={key++}
          className="cite-chip"
          onClick={() => onCite(index)}
          title={`Show source ${index}`}
        >
          {index}
        </button>,
      );
    } else if (token.startsWith("`")) {
      out.push(<code key={key++}>{token.slice(1, -1)}</code>);
    } else if (token.startsWith("**")) {
      out.push(<strong key={key++}>{token.slice(2, -2)}</strong>);
    } else if (token.startsWith("*")) {
      out.push(<em key={key++}>{token.slice(1, -1)}</em>);
    } else {
      const link = token.match(/^\[([^\]]+)\]\(([^)\s]+)\)$/);
      if (link && /^https?:\/\//i.test(link[2])) {
        // Only http(s) becomes a link: a model-authored javascript: or data:
        // URL must never be clickable.
        out.push(
          <a key={key++} href={link[2]} target="_blank" rel="noreferrer noopener">
            {link[1]}
          </a>,
        );
      } else {
        out.push(<Fragment key={key++}>{token}</Fragment>);
      }
    }
    rest = rest.slice(match.index + token.length);
  }
  return out;
}
