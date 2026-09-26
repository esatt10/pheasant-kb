import { useState } from "react";
import type { Overview } from "../api/types";
import { QuickAdd } from "./QuickAdd";
import { CopyLine } from "./CopyLine";
import { UploadDrop } from "./UploadDrop";

/**
 * What you see when nothing is indexed.
 *
 * This replaces the failure mode the redesign started from: with an empty
 * corpus the canvas rendered a single knowledge-base node floating alone,
 * which looks like a broken graph rather than an empty one. Saying so plainly
 * — and offering the two ways to fix it — is more useful than a lone star.
 */
export function EmptyState({
  overview,
  onAdded,
}: {
  overview: Overview;
  onAdded: () => void;
}) {
  const [adding, setAdding] = useState(false);
  const hasSources = overview.source_count > 0;

  return (
    <div className="page">
      <div className="card empty-state">
        <h2>{hasSources ? "Nothing indexed yet" : "Add your first source"}</h2>
        <p>
          {hasSources
            ? `“${overview.name}” has ${overview.source_count} configured source${
                overview.source_count === 1 ? "" : "s"
              }, but no files have been indexed. The paths may be empty or not mounted into the container.`
            : `“${overview.name}” is running and empty. Point it at anything — a folder, a repository, a docs site — and it becomes searchable and answerable.`}
        </p>

        <div className="empty-state__steps">
          <div className="step">
            <span className="step__n">1</span>
            <div className="step__body">
              <div className="step__title">Add a source from here</div>
              <p className="muted small" style={{ margin: "0 0 8px" }}>
                Paste a path or URL and pheasant detects the rest.
              </p>
              <button className="btn btn--primary" onClick={() => setAdding(true)}>
                Add a source
              </button>
            </div>
          </div>

          <div className="step">
            <span className="step__n">2</span>
            <div className="step__body">
              <div className="step__title">…or just drop some files in</div>
              <p className="muted small" style={{ margin: "0 0 8px" }}>
                No path to type, no directory to mount. Uploaded files and ZIP
                archives become a normal source and are indexed like any other.
              </p>
              <UploadDrop onUploaded={onAdded} />
            </div>
          </div>

          <div className="step">
            <span className="step__n">3</span>
            <div className="step__body">
              <div className="step__title">…or do it in one command</div>
              <p className="muted small" style={{ margin: "0 0 8px" }}>
                Detects, configures, indexes and serves — any target, one line.
              </p>
              <CopyLine command="pheasant up ~/notes https://github.com/you/project" />
              <p className="muted small" style={{ margin: "8px 0 0" }}>
                Run it in a container instead with{" "}
                <code>pheasant host ~/notes</code>, which writes a compose file and
                brings the stack up.
              </p>
            </div>
          </div>

          {hasSources ? (
            <div className="step">
              <span className="step__n">4</span>
              <div className="step__body">
                <div className="step__title">Check your mounts</div>
                <p className="muted small" style={{ margin: 0 }}>
                  Configured sources: {overview.sources.map((s) => s.path).join(", ")}. If
                  you are running in Docker, each of these must be mounted into the
                  container — an unmounted path indexes zero files without erroring.
                  Run <code>pheasant mount &lt;host-path&gt;</code> to write the bind
                  mount, or paste the path into "Add a source" and pheasant will tell
                  you the exact line to add.
                </p>
              </div>
            </div>
          ) : null}
        </div>
      </div>

      {adding ? <QuickAdd onClose={() => setAdding(false)} onAdded={onAdded} /> : null}
    </div>
  );
}
