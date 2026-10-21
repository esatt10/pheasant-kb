import DOMPurify from "dompurify";
import { Marked } from "marked";
import { useMemo } from "react";
import { mathExtension, renderMath } from "../../util/math";
import { highlightLines, languageFor } from "../highlight";

/** Markdown cells, with the TeX a notebook usually carries typeset. */
const markdown = new Marked(mathExtension, { gfm: true });

interface Cell {
  cell_type: string;
  source: string | string[];
  execution_count?: number | null;
  outputs?: Output[];
}

interface Output {
  output_type: string;
  text?: string | string[];
  name?: string;
  data?: Record<string, string | string[]>;
  ename?: string;
  evalue?: string;
  traceback?: string[];
}

const IMAGE_TYPES = ["image/png", "image/jpeg", "image/gif"];

/** A notebook's cells, or `null` when the text is not one. */
export function parseNotebook(text: string): { cells: Cell[]; language: string } | null {
  try {
    const parsed = JSON.parse(text) as {
      cells?: Cell[];
      metadata?: { language_info?: { name?: string }; kernelspec?: { language?: string } };
    };
    if (!Array.isArray(parsed.cells)) return null;
    const language =
      parsed.metadata?.language_info?.name ?? parsed.metadata?.kernelspec?.language ?? "python";
    return { cells: parsed.cells, language };
  } catch {
    return null;
  }
}

/**
 * A Jupyter notebook as cells: Markdown rendered, code highlighted with its
 * execution count, and outputs — text, errors (ANSI colour stripped) and
 * embedded raster images, which are data the notebook already carries rather
 * than anything fetched. HTML outputs are shown as their plain-text twin.
 */
export function NotebookView({ text }: { text: string }) {
  const notebook = useMemo(() => parseNotebook(text), [text]);
  if (!notebook) return <p className="muted small viewer-pad">This notebook could not be parsed.</p>;
  const language = languageFor(notebook.language);
  return (
    <div className="nb-view">
      {notebook.cells.map((cell, index) => (
        <NotebookCell key={index} cell={cell} language={language} />
      ))}
    </div>
  );
}

function NotebookCell({ cell, language }: { cell: Cell; language: string }) {
  const source = joined(cell.source);
  if (cell.cell_type === "markdown") {
    const html = renderMath(
      DOMPurify.sanitize(markdown.parse(source, { async: false }) as string, {
        USE_PROFILES: { html: true },
        FORBID_TAGS: ["img", "style", "iframe", "form", "video", "audio", "source", "picture"],
        FORBID_ATTR: ["style", "srcset"],
      }),
    );
    return <div className="nb-cell nb-cell--markdown md-view" dangerouslySetInnerHTML={{ __html: html }} />;
  }
  if (cell.cell_type !== "code") {
    return <pre className="nb-cell nb-cell--raw">{source}</pre>;
  }
  const lines = highlightLines(source, language);
  return (
    <div className="nb-cell nb-cell--code">
      <div className="nb-cell__prompt">[{cell.execution_count ?? " "}]</div>
      <div className="nb-cell__main">
        <pre className="nb-cell__source">
          <code
            className="hljs"
            dangerouslySetInnerHTML={{
              __html: DOMPurify.sanitize(lines.join("\n"), { ALLOWED_TAGS: ["span"], ALLOWED_ATTR: ["class"] }),
            }}
          />
        </pre>
        {(cell.outputs ?? []).map((output, index) => (
          <NotebookOutput key={index} output={output} />
        ))}
      </div>
    </div>
  );
}

function NotebookOutput({ output }: { output: Output }) {
  if (output.output_type === "stream") {
    return (
      <pre className={`nb-output${output.name === "stderr" ? " nb-output--error" : ""}`}>
        {stripAnsi(joined(output.text))}
      </pre>
    );
  }
  if (output.output_type === "error") {
    return (
      <pre className="nb-output nb-output--error">
        {stripAnsi((output.traceback ?? [`${output.ename}: ${output.evalue}`]).join("\n"))}
      </pre>
    );
  }
  const data = output.data ?? {};
  const image = IMAGE_TYPES.find((type) => typeof data[type] === "string" || Array.isArray(data[type]));
  if (image) {
    const payload = joined(data[image]).replace(/\s+/g, "");
    if (/^[A-Za-z0-9+/=]+$/.test(payload)) {
      return <img className="nb-output__image" src={`data:${image};base64,${payload}`} alt="cell output" />;
    }
  }
  const plain = data["text/plain"];
  if (plain !== undefined) return <pre className="nb-output">{stripAnsi(joined(plain))}</pre>;
  return null;
}

function joined(value: string | string[] | undefined): string {
  return Array.isArray(value) ? value.join("") : value ?? "";
}

function stripAnsi(text: string): string {
  return text.replace(/\u001b\[[0-9;]*[A-Za-z]/g, "");
}
