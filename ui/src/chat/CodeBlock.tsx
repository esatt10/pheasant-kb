import { useEffect, useState } from "react";

/**
 * A fenced block in an answer: its language, a copy button, and the code in
 * pheasant's syntax colours -- the source viewer's highlighter
 * (`browser/highlight.ts`), loaded the first time an answer holds code so the
 * chat does not carry it until then. Highlighted output is highlight.js's,
 * which escapes the code; until it arrives the block is plain text.
 */
export function CodeBlock({ language, code }: { language: string; code: string }) {
  const [html, setHtml] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    let live = true;
    setHtml(null);
    import("../browser/highlight").then(({ highlightLines, languageFor }) => {
      if (live) setHtml(highlightLines(code, languageFor(language)).join("\n"));
    });
    return () => {
      live = false;
    };
  }, [code, language]);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  };
  return (
    <div className="answer-code-block">
      <div className="answer-code-block__bar">
        <span>{language || "code"}</span>
        <button type="button" className="btn btn--small btn--ghost" onClick={copy} title="Copy this block">
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <pre className="answer-code" data-language={language || undefined}>
        {html === null ? <code>{code}</code> : <code dangerouslySetInnerHTML={{ __html: html }} />}
      </pre>
    </div>
  );
}
