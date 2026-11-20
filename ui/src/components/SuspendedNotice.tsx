import { useEffect, useState } from "react";
import { api, onAccessSuspended, setApiToken } from "../api/client";

/**
 * What a person sees when the operator has suspended access (the kill switch,
 * `ACCESS_SUSPENDED`): the region's own sentence, with the operator's reason,
 * over the whole page, instead of every panel failing on its own. Shown the
 * first time any request is refused that way; "Try again" reloads, and "Sign
 * out" ends the session so another account can sign in.
 */
export function SuspendedNotice() {
  const [message, setMessage] = useState<string | null>(null);
  useEffect(() => onAccessSuspended((text) => setMessage((shown) => shown ?? text)), []);
  if (!message) return null;
  return (
    <div className="suspended-notice" role="alertdialog" aria-modal="true" aria-labelledby="suspended-title">
      <div className="suspended-notice__card">
        <h1 id="suspended-title">Access is paused</h1>
        <p>{message}</p>
        <div className="suspended-notice__actions">
          <button type="button" className="btn btn--primary" onClick={() => window.location.reload()}>
            Try again
          </button>
          <button
            type="button"
            className="btn"
            onClick={() => {
              api
                .signOut()
                .catch(() => undefined)
                .finally(() => {
                  setApiToken("");
                  window.location.reload();
                });
            }}
          >
            Sign out
          </button>
        </div>
      </div>
    </div>
  );
}
