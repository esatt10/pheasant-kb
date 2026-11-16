/**
 * The pheasant wordmark.
 *
 * The full-colour illustration keeps the reference image's white background
 * and does not inherit `currentColor`. It is served from `public/`, which is
 * also where the favicon link in `index.html` points, so the tab icon and the
 * in-app mark use the same artwork.
 */
export function PheasantMark({ size = 30 }: { size?: number }) {
  return (
    <img
      src="/pheasant.png"
      width={size}
      height={size}
      alt=""
      aria-hidden
      decoding="async"
    />
  );
}
