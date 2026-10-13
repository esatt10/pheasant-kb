import { useSession } from "../state/session";

/**
 * "Search only here": answer questions from this folder or file alone.
 *
 * The taxonomy view's version of selecting a source in the rail, one level
 * finer. At a source's root it *is* the source toggle (the same
 * `sourceFilter`), so the two controls can never disagree; in a folder or on
 * a file it sets `pathScope`, which also selects the source. Click again to
 * search the whole source; the composer shows the scope and clears it too.
 */
export function ScopeToggle({
  source,
  path,
  kind,
}: {
  source: string;
  path: string;
  kind: "folder" | "file";
}) {
  const { state, dispatch } = useSession();
  const scope = state.pathScope;
  const wholeSource = kind === "folder" && !path;
  const active = wholeSource
    ? state.sourceFilter === source && !scope
    : scope?.source === source && scope.path === path;
  const where = wholeSource ? source : `${source}/${path}`;
  const label = active
    ? "Searching only here"
    : wholeSource
      ? "Search this source"
      : kind === "file"
        ? "Search this file"
        : "Search this folder";

  const toggle = () => {
    if (wholeSource) {
      dispatch({ type: "set-path-scope", scope: null });
      dispatch({ type: "filter-source", source: active ? null : source });
    } else {
      dispatch({ type: "set-path-scope", scope: active ? null : { source, path, kind } });
    }
  };

  return (
    <button
      type="button"
      className={`scope-toggle${active ? " scope-toggle--active" : ""}`}
      aria-pressed={active}
      onClick={toggle}
      title={
        active
          ? `Questions are answered only from ${where}. Click to search ${
              wholeSource ? "every source" : `all of ${source}`
            } again.`
          : `Answer questions only from ${where}`
      }
    >
      <span className="scope-toggle__icon" aria-hidden>
        ⌖
      </span>
      {label}
    </button>
  );
}
