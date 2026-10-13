import type { AnswerScope as Scope } from "../api/types";

/**
 * Where an answer was allowed to look, under the answer: the folder or file
 * it was read from and the documents left out. Shown only when something
 * narrowed it, so an unscoped answer reads exactly as it always did. Notes
 * (`@current` with nothing open, a name that matched nothing) are already in
 * the answer's text.
 */
export function AnswerScope({ scope }: { scope: Scope }) {
  const where = scope.path_prefix
    ? `${scope.kind === "file" ? "from" : "in"} ${scope.source_name}/${scope.path_prefix}`
    : scope.source_name
      ? `in ${scope.source_name}`
      : null;
  if (!where && !scope.excluded.length) return null;
  const keywords = scope.keywords.length ? ` (${scope.keywords.join(" ")})` : "";
  return (
    <p className="msg__hint answer-scope">
      {where ? <span className="pill pill--accent">only {where}{keywords}</span> : null}
      {scope.excluded.length ? (
        <span className="pill" title={scope.excluded.join("\n")}>
          left out {scope.excluded.length === 1 ? scope.excluded[0] : `${scope.excluded.length} documents`}
        </span>
      ) : null}
    </p>
  );
}
