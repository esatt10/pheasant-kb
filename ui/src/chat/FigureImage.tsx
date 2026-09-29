import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { Figure } from "../api/types";

/**
 * An image the cited documents show, fetched through `/media`.
 *
 * Fetched rather than given to `<img src>` directly: a region with an API
 * token refuses a request that carries none, and an `<img>` cannot send one.
 * The object URL is revoked when the figure unmounts.
 */
export function FigureImage({ figure, onOpen }: { figure: Figure; onOpen?: () => void }) {
  const [url, setUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let revoked = false;
    let created: string | null = null;
    api
      .mediaObjectUrl(figure.node_id)
      .then((objectUrl) => {
        created = objectUrl;
        if (revoked) URL.revokeObjectURL(objectUrl);
        else setUrl(objectUrl);
      })
      .catch(() => setFailed(true));
    return () => {
      revoked = true;
      if (created) URL.revokeObjectURL(created);
    };
  }, [figure.node_id]);

  const caption = [figure.relative_path, figure.caption || figure.alt].filter(Boolean).join(" — ");
  return (
    <figure className="answer-figure">
      {url ? (
        <button type="button" className="answer-figure__open" onClick={onOpen} title="Show in graph">
          <img src={url} alt={figure.alt || figure.caption || figure.relative_path || "figure"} />
        </button>
      ) : (
        <div className="answer-figure__placeholder">
          {failed ? "Image unavailable" : "Loading image…"}
        </div>
      )}
      <figcaption>
        <span className="answer-figure__n">Fig. {figure.figure}</span> {caption}
      </figcaption>
    </figure>
  );
}
