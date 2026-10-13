import { useSession } from "../state/session";

/**
 * "Leave out of answers": the inverse of {@link ScopeToggle}, for one file.
 *
 * Adds the file to the session's `excluded` list, which every question
 * scoped to its source sends as `exclude_documents` — the same parameter an
 * agent passes, and the same thing `@exclude <name>` says in a message. The
 * composer shows the count and clears it.
 */
export function OmitToggle({ source, path }: { source: string; path: string }) {
  const { state, dispatch } = useSession();
  const document = `${source}/${path}`;
  const omitted = state.excluded.includes(document);
  const scoped = state.pathScope?.kind === "file" && state.pathScope.source === source && state.pathScope.path === path;
  return (
    <button
      type="button"
      className={`scope-toggle${omitted ? " scope-toggle--omitted" : ""}`}
      aria-pressed={omitted}
      disabled={scoped && !omitted}
      onClick={() => dispatch({ type: "toggle-excluded", document })}
      title={
        omitted
          ? `${document} is left out of every answer. Click to search it again.`
          : scoped
            ? "Questions are answered only from this file, so it cannot also be left out"
            : `Leave ${document} out of every answer`
      }
    >
      <span className="scope-toggle__icon" aria-hidden>
        ⊘
      </span>
      {omitted ? "Omitted from answers" : "Omit from answers"}
    </button>
  );
}
